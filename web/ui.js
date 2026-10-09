// 4.0 没有旧的下载页插槽。首页小组件只负责把脚本留下来，真正的按钮插进现有页面。

const CD2_CONFIG = "/api/v1/extensions/cd2/config";

async function call(method, url, body) {
  const response = await fetch(url, {
    method,
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const message = data.msg_zh || data.detail || data.msg_en || "请求失败";
    throw new Error(typeof message === "string" ? message : "请求失败");
  }
  return data;
}

function hideSlot(element) {
  const body = element.getRootNode()?.host;
  const slot = body?.closest?.(".plugin-slot");
  if (!slot) return;
  slot.style.display = "none";
  const wrap = slot.closest(".plugin-widgets");
  if (
    wrap &&
    [...wrap.querySelectorAll(".plugin-slot")].every(
      (node) => node.style.display === "none"
    )
  ) {
    wrap.style.display = "none";
  }
}

async function testConnection(host, button) {
  button.disabled = true;
  try {
    const saved = await call("GET", CD2_CONFIG);
    const typed = document.querySelector('input[aria-label="密码"]')?.value?.trim();
    const result = await call("POST", `${CD2_CONFIG}/test`, {
      host: saved.host || "",
      username: saved.username || "",
      password: typed || saved.password || "",
    });
    host?.toast?.(
      result.msg_zh || result.msg_en || "",
      result.success ? "success" : "error"
    );
  } catch (error) {
    host?.toast?.(error.message || "连接失败", "error");
  } finally {
    button.disabled = false;
  }
}

function ensureTest(host) {
  for (const title of document.querySelectorAll(".plugin__title")) {
    if (!title.textContent.includes("CloudDrive2")) continue;
    let card = title;
    for (let depth = 0; depth < 8 && card; depth += 1) {
      const save = card.querySelector?.(":scope > .plugin__options .plugin__save, .plugin__save");
      if (save && card.querySelector(".plugin__title") === title) {
        if (card.querySelector("[data-cd2-test]")) return;
        const button = document.createElement("button");
        button.dataset.cd2Test = "1";
        button.type = "button";
        button.textContent = "测试连接";
        matchButton(button, save);
        button.style.background = "var(--color-surface-2)";
        button.style.color = "var(--color-text)";
        button.style.marginRight = "8px";
        save.before(button);
        button.addEventListener("click", () => testConnection(host, button));
        return;
      }
      card = card.parentElement;
    }
  }
}

function selectedNames() {
  const names = [];
  document.querySelectorAll("tbody tr").forEach((row) => {
    if (!row.querySelector(".n-checkbox--checked")) return;
    const name = row.querySelector('[data-col-key="name"]')?.textContent?.trim();
    if (name) names.push(name);
  });
  return names;
}

async function repair(host, button) {
  const names = selectedNames();
  if (names.length === 0) {
    host?.toast?.("先选中要修复的任务", "info");
    return;
  }
  button.disabled = true;
  try {
    const saved = await call("GET", CD2_CONFIG);
    if (!saved.enable) {
      host?.toast?.("请先在设置中启用 CD2 回退", "error");
      return;
    }
    const torrents = await call("GET", "/api/v1/downloader/torrents");
    const list = Array.isArray(torrents) ? torrents : [];
    const wanted = new Set(names);
    const hashes = list
      .filter((item) => wanted.has(String(item.name || "").trim()))
      .map((item) => item.hash)
      .filter(Boolean);
    if (hashes.length === 0) {
      host?.toast?.("没有对上选中的任务", "error");
      return;
    }
    const result = await call(
      "POST",
      "/api/v1/extensions/cd2/downloader/torrents/cd2-repair",
      { hashes }
    );
    const message = result.msg_zh || result.msg_en || "已发送修复";
    host?.toast?.(message, result.success === false ? "error" : "success");
  } catch (error) {
    host?.toast?.(error.message || "修复失败", "error");
  } finally {
    button.disabled = false;
  }
}

function matchButton(button, sample) {
  const style = getComputedStyle(sample);
  button.style.boxSizing = "border-box";
  button.style.display = "inline-flex";
  button.style.alignItems = "center";
  button.style.justifyContent = "center";
  button.style.height = style.height;
  button.style.minHeight = style.height;
  button.style.padding = style.padding;
  button.style.margin = "0";
  button.style.flex = style.flex;
  button.style.fontSize = style.fontSize;
  button.style.fontWeight = style.fontWeight;
  button.style.fontFamily = style.fontFamily;
  button.style.lineHeight = style.lineHeight;
  button.style.borderRadius = style.borderRadius;
  button.style.border = style.border;
  button.style.background = style.backgroundColor;
  button.style.color = style.color;
  button.style.whiteSpace = "nowrap";
  button.style.cursor = "pointer";
}

function ensureRepair(host) {
  const bar = document.querySelector(".action-bar-buttons");
  if (!bar || bar.querySelector("[data-cd2-repair]")) return;
  const sample = bar.querySelectorAll("button")[1] || bar.querySelector("button");
  if (!sample) return;
  const button = document.createElement("button");
  button.dataset.cd2Repair = "1";
  button.type = "button";
  button.textContent = "CD2 修复";
  matchButton(button, sample);
  button.addEventListener("click", () => repair(host, button));
  bar.appendChild(button);
}

let activeHost = { toast() {} };

function installCd2(host) {
  if (host) activeHost = host;
  if (window.__cd2Ui) return;
  window.__cd2Ui = true;
  const tick = () => {
    ensureTest(activeHost);
    ensureRepair(activeHost);
  };
  tick();
  new MutationObserver(tick).observe(document.body, { childList: true, subtree: true });
}

class Cd2Boot extends HTMLElement {
  connectedCallback() {
    hideSlot(this);
    installCd2(this.host);
  }
}

if (!customElements.get("ab-plugin-cd2-boot")) {
  customElements.define("ab-plugin-cd2-boot", Cd2Boot);
}

installCd2(activeHost);
