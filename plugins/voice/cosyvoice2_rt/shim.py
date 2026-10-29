"""diffusers 最小子集 + Matcha-TTS 组件（MIT, shivammehta25/Matcha-TTS）。

CosyVoice2 的 CausalConditionalDecoder 依赖 matcha 的 1D 组件，而 matcha 又依赖
diffusers 的 GELU/GEGLU/Attention/LoRACompatibleLinear/get_activation。这里按
diffusers 的参数名与形状实现最小子集（state dict 键兼容：``ff.net.0.proj``、
``attn1.to_q`` 等键与官方 checkpoint 逐一对上），免掉整个 diffusers 依赖。
"""
import math
from typing import Optional

import torch
import torch.nn as nn
import torch.nn.functional as F
from einops import pack, rearrange, repeat


# ---------------------------------------------------------------------------
# diffusers 最小子集
# ---------------------------------------------------------------------------

def maybe_allow_in_graph(cls):
    return cls


def get_activation(act_fn: str) -> nn.Module:
    return {"silu": nn.SiLU, "swish": nn.SiLU, "gelu": nn.GELU, "mish": nn.Mish}[act_fn]()


#: diffusers 旧类名；参数名/形状与 nn.Linear 完全一致
LoRACompatibleLinear = nn.Linear


class GELU(nn.Module):
    """diffusers GELU：先线性投影再 gelu（键 ``ff.net.0.proj``）。"""

    def __init__(self, dim_in: int, dim_out: int, approximate: str = "none", bias: bool = True):
        super().__init__()
        self.proj = nn.Linear(dim_in, dim_out, bias=bias)
        self.approximate = approximate

    def gelu(self, gate: torch.Tensor) -> torch.Tensor:
        return F.gelu(gate, approximate=self.approximate)

    def forward(self, hidden_states: torch.Tensor) -> torch.Tensor:
        hidden_states = self.proj(hidden_states)
        return self.gelu(hidden_states)


class GEGLU(nn.Module):
    """diffusers GEGLU：投影出 2 倍宽再门控（键 ``ff.net.0.proj``）。"""

    def __init__(self, dim_in: int, dim_out: int, bias: bool = True):
        super().__init__()
        self.proj = nn.Linear(dim_in, dim_out * 2, bias=bias)

    def gelu(self, gate: torch.Tensor) -> torch.Tensor:
        return F.gelu(gate)

    def forward(self, hidden_states: torch.Tensor) -> torch.Tensor:
        hidden_proj = self.proj(hidden_states)
        hidden, gate = hidden_proj.chunk(2, dim=-1)
        return hidden * self.gelu(gate)


class Attention(nn.Module):
    """diffusers Attention 最小实现（AttnProcessor2_0 语义：SDPA + 加性 mask）。

    键与 diffusers 一致：``to_q/to_k/to_v``、``to_out.0``。attention_mask 接受
    (b, kv) 或 (b, q, kv) 的加性 float mask。
    """

    def __init__(self, query_dim: int, cross_attention_dim: Optional[int] = None,
                 heads: int = 8, dim_head: int = 64, dropout: float = 0.0,
                 bias: bool = False, upcast_attention: bool = False, **kwargs):
        super().__init__()
        inner = heads * dim_head
        self.heads = heads
        self.scale = dim_head ** -0.5
        self.upcast_attention = upcast_attention
        kv_dim = cross_attention_dim or query_dim
        self.to_q = nn.Linear(query_dim, inner, bias=bias)
        self.to_k = nn.Linear(kv_dim, inner, bias=bias)
        self.to_v = nn.Linear(kv_dim, inner, bias=bias)
        # 注意：官方 checkpoint（diffusers 旧版 Attention）只给 to_out 投影带 bias，q/k/v 不带
        self.to_out = nn.ModuleList([nn.Linear(inner, query_dim, bias=True), nn.Dropout(dropout)])

    def forward(self, hidden_states: torch.Tensor, encoder_hidden_states: Optional[torch.Tensor] = None,
                attention_mask: Optional[torch.Tensor] = None, **kwargs) -> torch.Tensor:
        query = self.to_q(hidden_states)
        key = self.to_k(encoder_hidden_states if encoder_hidden_states is not None else hidden_states)
        value = self.to_v(encoder_hidden_states if encoder_hidden_states is not None else hidden_states)
        b, tq, _ = query.shape
        tk = key.shape[1]
        query = query.view(b, tq, self.heads, -1).transpose(1, 2)
        key = key.view(b, tk, self.heads, -1).transpose(1, 2)
        value = value.view(b, tk, self.heads, -1).transpose(1, 2)
        if attention_mask is not None and attention_mask.dim() == 3:
            # (b, tq, tk) 加性 mask → (b, 1, tq, tk) 广播到各 head
            attention_mask = attention_mask.unsqueeze(1)
        out = F.scaled_dot_product_attention(query, key, value, attn_mask=attention_mask,
                                             scale=self.scale)
        out = out.transpose(1, 2).reshape(b, tq, -1)
        return self.to_out[0](out)


class FeedForward(nn.Module):
    """matcha 版 FeedForward（``net.0`` 是激活模块本身，``net.2`` 是输出投影）。"""

    def __init__(self, dim: int, dim_out: Optional[int] = None, mult: int = 4,
                 dropout: float = 0.0, activation_fn: str = "geglu", final_dropout: bool = False):
        super().__init__()
        inner_dim = int(dim * mult)
        dim_out = dim_out if dim_out is not None else dim
        if activation_fn == "gelu":
            act_fn = GELU(dim, inner_dim)
        elif activation_fn == "gelu-approximate":
            act_fn = GELU(dim, inner_dim, approximate="tanh")
        elif activation_fn == "geglu":
            act_fn = GEGLU(dim, inner_dim)
        else:
            raise ValueError(f"unsupported activation_fn {activation_fn!r} (snakebeta 未 vendor)")
        self.net = nn.ModuleList([act_fn, nn.Dropout(dropout), nn.Linear(inner_dim, dim_out)])
        if final_dropout:
            self.net.append(nn.Dropout(dropout))

    def forward(self, hidden_states: torch.Tensor) -> torch.Tensor:
        for module in self.net:
            hidden_states = module(hidden_states)
        return hidden_states


# ---------------------------------------------------------------------------
# Matcha-TTS 组件（decoder.py / transformer.py 的推理子集）
# ---------------------------------------------------------------------------

class SinusoidalPosEmb(torch.nn.Module):
    def __init__(self, dim):
        super().__init__()
        self.dim = dim
        assert self.dim % 2 == 0, "SinusoidalPosEmb requires dim to be even"

    def forward(self, x, scale=1000):
        if x.ndim < 1:
            x = x.unsqueeze(0)
        device = x.device
        half_dim = self.dim // 2
        emb = math.log(10000) / (half_dim - 1)
        emb = torch.exp(torch.arange(half_dim, device=device).float() * -emb)
        emb = scale * x.unsqueeze(1) * emb.unsqueeze(0)
        emb = torch.cat((emb.sin(), emb.cos()), dim=-1)
        return emb


class Block1D(torch.nn.Module):
    def __init__(self, dim, dim_out, groups=8):
        super().__init__()
        self.block = torch.nn.Sequential(
            torch.nn.Conv1d(dim, dim_out, 3, padding=1),
            torch.nn.GroupNorm(groups, dim_out),
            nn.Mish(),
        )

    def forward(self, x, mask):
        output = self.block(x * mask)
        return output * mask


class ResnetBlock1D(torch.nn.Module):
    def __init__(self, dim, dim_out, time_emb_dim, groups=8):
        super().__init__()
        self.mlp = torch.nn.Sequential(nn.Mish(), torch.nn.Linear(time_emb_dim, dim_out))
        self.block1 = Block1D(dim, dim_out, groups=groups)
        self.block2 = Block1D(dim_out, dim_out, groups=groups)
        self.res_conv = torch.nn.Conv1d(dim, dim_out, 1)

    def forward(self, x, mask, time_emb):
        h = self.block1(x, mask)
        h += self.mlp(time_emb).unsqueeze(-1)
        h = self.block2(h, mask)
        return h + self.res_conv(x * mask)


class Downsample1D(nn.Module):
    def __init__(self, dim):
        super().__init__()
        self.conv = torch.nn.Conv1d(dim, dim, 3, 2, 1)

    def forward(self, x):
        return self.conv(x)


class TimestepEmbedding(nn.Module):
    def __init__(self, in_channels: int, time_embed_dim: int, act_fn: str = "silu",
                 out_dim: int = None, post_act_fn: Optional[str] = None, cond_proj_dim=None):
        super().__init__()
        self.linear_1 = nn.Linear(in_channels, time_embed_dim)
        self.cond_proj = nn.Linear(cond_proj_dim, in_channels, bias=False) if cond_proj_dim is not None else None
        self.act = get_activation(act_fn)
        self.linear_2 = nn.Linear(time_embed_dim, out_dim or time_embed_dim)
        self.post_act = get_activation(post_act_fn) if post_act_fn is not None else None

    def forward(self, sample, condition=None):
        if condition is not None:
            sample = sample + self.cond_proj(condition)
        sample = self.linear_1(sample)
        if self.act is not None:
            sample = self.act(sample)
        sample = self.linear_2(sample)
        if self.post_act is not None:
            sample = self.post_act(sample)
        return sample


class Upsample1D(nn.Module):
    def __init__(self, channels, use_conv=False, use_conv_transpose=True, out_channels=None, name="conv"):
        super().__init__()
        self.channels = channels
        self.out_channels = out_channels or channels
        self.use_conv = use_conv
        self.use_conv_transpose = use_conv_transpose
        self.name = name
        if use_conv_transpose:
            self.conv = nn.ConvTranspose1d(channels, self.out_channels, 4, 2, 1)
        elif use_conv:
            self.conv = nn.Conv1d(self.channels, self.out_channels, 3, padding=1)

    def forward(self, inputs):
        assert inputs.shape[1] == self.channels
        if self.use_conv_transpose:
            return self.conv(inputs)
        outputs = F.interpolate(inputs, scale_factor=2.0, mode="nearest")
        if self.use_conv:
            outputs = self.conv(outputs)
        return outputs


class BasicTransformerBlock(nn.Module):
    """matcha 版本；ada-norm 分支未 vendor（CosyVoice2 全部走默认 layer_norm 路径）。"""

    def __init__(self, dim: int, num_attention_heads: int, attention_head_dim: int,
                 dropout=0.0, cross_attention_dim: Optional[int] = None,
                 activation_fn: str = "geglu", attention_bias: bool = False,
                 double_self_attention: bool = False, norm_elementwise_affine: bool = True):
        super().__init__()
        self.norm1 = nn.LayerNorm(dim, elementwise_affine=norm_elementwise_affine)
        self.attn1 = Attention(
            query_dim=dim, heads=num_attention_heads, dim_head=attention_head_dim,
            dropout=dropout, bias=attention_bias,
        )
        if cross_attention_dim is not None or double_self_attention:
            self.norm2 = nn.LayerNorm(dim, elementwise_affine=norm_elementwise_affine)
            self.attn2 = Attention(
                query_dim=dim, cross_attention_dim=None if double_self_attention else cross_attention_dim,
                heads=num_attention_heads, dim_head=attention_head_dim,
                dropout=dropout, bias=attention_bias,
            )
        else:
            self.norm2 = None
            self.attn2 = None
        self.norm3 = nn.LayerNorm(dim, elementwise_affine=norm_elementwise_affine)
        self.ff = FeedForward(dim, dropout=dropout, activation_fn=activation_fn)

    def forward(self, hidden_states: torch.FloatTensor, attention_mask: Optional[torch.FloatTensor] = None,
                encoder_hidden_states: Optional[torch.FloatTensor] = None,
                encoder_attention_mask: Optional[torch.FloatTensor] = None,
                timestep: Optional[torch.LongTensor] = None, **kwargs) -> torch.FloatTensor:
        norm_hidden_states = self.norm1(hidden_states)
        attn_output = self.attn1(norm_hidden_states, attention_mask=attention_mask)
        hidden_states = attn_output + hidden_states
        if self.attn2 is not None:
            norm_hidden_states = self.norm2(hidden_states)
            attn_output = self.attn2(norm_hidden_states,
                                     encoder_hidden_states=encoder_hidden_states,
                                     attention_mask=encoder_attention_mask)
            hidden_states = attn_output + hidden_states
        norm_hidden_states = self.norm3(hidden_states)
        ff_output = self.ff(norm_hidden_states)
        return ff_output + hidden_states


class BASECFM(torch.nn.Module):
    """matcha flow_matching 基类；CosyVoice 的 ConditionalCFM 覆写了 forward/solve_euler。"""

    def __init__(self, n_feats: int, cfm_params, n_spks: int = 1, spk_emb_dim: int = 128):
        super().__init__()
        self.n_feats = n_feats
        self.n_spks = n_spks
        self.spk_emb_dim = spk_emb_dim
        self.solver = cfm_params.solver
        self.sigma_min = cfm_params.sigma_min if hasattr(cfm_params, "sigma_min") else 1e-4
        self.estimator = None
