/**
 * output 产物的 URL 构造与媒体地址解析。
 *
 * 为什么需要这个模块：产物文件名由提示词生成（见后端 _slugify），会包含中文、空格、
 * 以及 `#` `%` `&` 等在 URL 里有特殊语义的字符。直接写 `"/output/" + name`
 * 会让浏览器把 `#` 之后的部分当成锚点丢掉 —— 项目 output/ 里确实存在
 * `result_#_《头条…》.json` 这类文件，其"归档 JSON"链接因此一直是坏的。
 */

/**
 * 把 output 目录内的相对文件名转成可安全放进 href/src 的 URL。
 * 逐段编码，保留路径分隔符 `/`（产物可能位于子目录）。
 *
 * @param {unknown} name output 目录内的相对路径，如 "asset_x.png" 或 "sub/dir/a.png"
 * @returns {string|null} 形如 "/output/asset_x.png" 的 URL；输入非法时返回 null
 */
export function outUrl(name) {
  if (typeof name !== "string") return null;
  const raw = name.trim().replace(/\\/g, "/");
  if (!raw) return null;
  // 防御：绝对路径 / 盘符 / 上跳都不该出现在归档字段里，出现即视为脏数据
  if (raw.startsWith("/") || /^[a-zA-Z]:\//.test(raw) || raw.split("/").includes("..")) {
    return null;
  }
  return "/output/" + raw.split("/").map(encodeURIComponent).join("/");
}

/**
 * 解析一条媒体成品的可渲染地址。
 *
 * 优先级：归档后的本地文件 > 已是完整 http(s) URL > 已带 /output/ 前缀 > 裸相对文件名。
 * 无法渲染时返回 null，调用方应退回纯文本展示。
 *
 * @param {unknown} localFile 归档产生的 output 内相对文件名（可为空）
 * @param {unknown} rawOutput 原始 output 字段（可能是 URL、裸文件名或本机绝对路径）
 * @returns {string|null}
 */
export function mediaSrc(localFile, rawOutput) {
  const local = outUrl(localFile);
  if (local) return local;
  const o = typeof rawOutput === "string" ? rawOutput.trim() : "";
  if (!o) return null;
  if (/^https?:\/\//i.test(o)) return o;
  // 已带前缀的历史数据：仅补编码，避免二次加前缀
  if (o.startsWith("/output/")) return outUrl(o.slice("/output/".length));
  // 本机绝对路径（D:\... 或 /home/...）无法通过 HTTP 访问，退回纯文本
  if (/^[a-zA-Z]:[\\/]/.test(o) || o.startsWith("/")) return null;
  return outUrl(o);
}
