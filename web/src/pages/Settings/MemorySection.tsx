import { useEffect, useState } from "react";
import { Box } from "@mui/material";
import { Group, Hint, StatusLine, SwitchRow } from "../../components/SettingsKit";
import { settingsApi } from "../../api/client";

// 记忆轴总开关（runtime.knowledge.memory.enabled）。
// 单一总闸：关 = 不记录（L1 提炼 / L2 场景维护）也不再注入；历史数据保留。
export default function MemorySection() {
  const [enabled, setEnabled] = useState<boolean>(false);
  const [loaded, setLoaded] = useState(false);
  const [saved, setSaved] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    settingsApi.get().then((r) => {
      const d = r.data as Record<string, any>;
      const v = d?.runtime?.knowledge?.memory?.enabled;
      if (typeof v === "boolean") setEnabled(v);
    }).catch(() => {}).finally(() => setLoaded(true));
  }, []);

  const save = async (v: boolean) => {
    setError(""); setSaved("");
    try {
      await settingsApi.put({ runtime: { knowledge: { memory: { enabled: v } } } });
      setSaved("已保存，下次运行起按新开关运行");
    } catch (e) { setError((e as Error).message); }
  };

  return (
    <Group title="记忆" desc="记忆轴的总闸，管记忆全链路。">
      <SwitchRow
        title="启用记忆"
        desc="开启后，任务收尾提炼记忆（L1 atoms）、整理场景块（L2），并把记忆与她的自我画像注入后续任务的系统提示；关闭后不记录、不维护、不注入。"
        checked={enabled}
        disabled={!loaded}
        onChange={(v) => { setEnabled(v); void save(v); }}
      />
      <Hint>
        关闭 = 不记录也不再注入，<Box component="span" sx={{ fontFamily: "monospace" }}>~/.omniagent/</Box>
        下已产生的记忆数据不会删除，重新打开即恢复消费。检索记忆另有池子门槛（少于 20 条前零注入）。
      </Hint>
      <StatusLine saved={saved} error={error} />
    </Group>
  );
}
