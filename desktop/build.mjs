/**
 * esbuild 打包桌面壳（结构对齐 OmniAgent-v3）：
 * - main.ts → dist-electron/main.mjs（ESM，Electron 33 原生支持）
 * - preload.ts → dist-electron/preload.cjs（preload 必须是 CJS：沙箱不支持 ESM import）
 * js-yaml 等纯 JS 依赖直接打进产物，运行时零 node_modules 依赖。
 */
import { build } from "esbuild";

const common = {
  bundle: true,
  platform: "node",
  target: "node20",
  external: ["electron"],
  sourcemap: false,
  minify: false,
};

await build({ ...common, format: "esm", entryPoints: ["electron/main.ts"], outfile: "dist-electron/main.mjs" });
await build({ ...common, format: "cjs", entryPoints: ["electron/preload.ts"], outfile: "dist-electron/preload.cjs" });
await build({ ...common, format: "cjs", entryPoints: ["electron/titlebar-preload.ts"], outfile: "dist-electron/titlebar-preload.cjs" });
console.log("esbuild 完成: main.mjs + preload.cjs + titlebar-preload.cjs");
