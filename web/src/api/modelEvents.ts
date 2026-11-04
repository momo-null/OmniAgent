/**
 * 模型目录变更事件（前端进程内信号）。
 *
 * 为什么需要：模型目录的唯一真源在后端 `~/.omniagent/models.json`，而 Chat 的
 * 选择器只在挂载时拉一次；设置页是**遮罩**不是页面，关掉时 Chat 并不会重新挂载。
 * 所以「设置里改完目录」需要一个显式信号让在挂载的消费者去重新读一次。
 *
 * 不用状态库：这不是共享状态，只是一次「去重读」的广播；消费者仍然是后端的镜像。
 */
const MODELS_CHANGED_EVENT = "omni:models-changed";

/** 目录已变更（保存提供方 / 设为槽位默认后调用）。 */
export function notifyModelsChanged(): void {
  window.dispatchEvent(new CustomEvent(MODELS_CHANGED_EVENT));
}

/** 订阅变更，返回取消订阅函数（组件卸载时调用）。 */
export function onModelsChanged(handler: () => void): () => void {
  window.addEventListener(MODELS_CHANGED_EVENT, handler);
  return () => window.removeEventListener(MODELS_CHANGED_EVENT, handler);
}
