/*
 * chat.js — browser side of the chat page. Plain JavaScript, no framework, no build step.
 *
 * What it does:
 *   1. Renders messages: answers as Markdown (marked -> DOMPurify -> highlight.js),
 *      questions as plain text. Adds a "Копировать" button to every code block.
 *   2. Saves an answer (.md / .txt) or the whole chat (.md) in the browser (Blob + <a download>).
 *   3. Composer: Enter sends, Shift+Enter is a new line, "+" attaches files (chips with ✕).
 *   4. Sends the message with fetch(POST, FormData) and reads the server-sent events
 *      (SSE) from response.body by hand: `delta` -> append text, `done` -> footer with
 *      model/tokens/price, `error` -> banner (with "Повторить" when the server allows it).
 *      "Стоп" aborts the request (AbortController).
 *
 * Security rules (strict CSP: script-src 'self', style-src 'self'):
 *   - HTML from the model only ever reaches the page through DOMPurify.sanitize();
 *     everything else is inserted with textContent.
 *   - No inline styles in markup, no on*= attributes: listeners are attached here.
 */
(function () {
  "use strict";

  // ---------------------------------------------------------------------------------------
  // 0. Small helpers
  // ---------------------------------------------------------------------------------------

  const RENDER_INTERVAL_MS = 100; // redraw Markdown at most this often while streaming
  const MSG_TOO_LARGE = "Файлы слишком большие (не более 5 файлов по 10 МБ)";
  const MSG_NETWORK = "Нет связи с сервером. Проверьте подключение и попробуйте ещё раз.";
  const MSG_BROKEN = "Соединение прервалось";
  const MSG_GENERIC = "Не удалось отправить сообщение";

  function $(selector, root) {
    return (root || document).querySelector(selector);
  }

  /** CSRF token: the same value HTMX sends, taken from <body hx-headers='{...}'>. */
  function csrfToken() {
    try {
      return JSON.parse(document.body.getAttribute("hx-headers") || "{}")["X-CSRF-Token"] || "";
    } catch (e) {
      return "";
    }
  }

  // ---------------------------------------------------------------------------------------
  // 1. Rendering
  // ---------------------------------------------------------------------------------------

  marked.use({ gfm: true, breaks: true });

  // Every link in a rendered answer opens in a new tab and leaks nothing to the target.
  DOMPurify.addHook("afterSanitizeAttributes", function (node) {
    if (node.tagName === "A" && node.hasAttribute("href")) {
      node.setAttribute("target", "_blank");
      node.setAttribute("rel", "noopener noreferrer");
    }
  });

  const PURIFY_OPTIONS = {
    USE_PROFILES: { html: true },
    // Inline styles would violate the CSP; images from the model are not shown;
    // form controls have no business inside an answer.
    FORBID_TAGS: ["style", "img", "form", "input", "button", "textarea", "select"],
    FORBID_ATTR: ["style"],
  };

  /** Raw text of a message lives in its <template class="md"> (filled by the server). */
  function getSource(msg) {
    return $("template.md", msg).content.textContent;
  }

  function setSource(msg, text) {
    $("template.md", msg).content.textContent = text;
  }

  function addCopyButton(pre) {
    const code = $("code", pre);
    const button = document.createElement("button");
    button.type = "button";
    button.className = "copy";
    button.textContent = "Копировать";
    button.addEventListener("click", function () {
      navigator.clipboard.writeText((code || pre).textContent).then(function () {
        button.textContent = "Скопировано";
        setTimeout(function () { button.textContent = "Копировать"; }, 1500);
      });
    });
    pre.insertBefore(button, pre.firstChild);
  }

  /** Draw a message body from its source text. */
  function renderMessage(msg) {
    const body = $(".body", msg);
    const text = getSource(msg);
    if (msg.dataset.role !== "assistant") {
      body.textContent = text; // the user's own text: never interpreted as HTML
      return;
    }
    body.innerHTML = DOMPurify.sanitize(marked.parse(text), PURIFY_OPTIONS);
    body.querySelectorAll("pre").forEach(function (pre) {
      const code = $("code", pre);
      if (code) hljs.highlightElement(code);
      addCopyButton(pre);
    });
  }

  /** Re-render at most once per RENDER_INTERVAL_MS; flush() draws immediately. */
  function throttledRenderer(msg) {
    let timer = null;
    return {
      schedule: function () {
        if (timer === null) {
          timer = setTimeout(function () { timer = null; renderMessage(msg); }, RENDER_INTERVAL_MS);
        }
      },
      flush: function () {
        if (timer !== null) { clearTimeout(timer); timer = null; }
        renderMessage(msg);
      },
    };
  }

  // ---------------------------------------------------------------------------------------
  // 2. Saving in the browser
  // ---------------------------------------------------------------------------------------

  function download(filename, text, mime) {
    const blob = new Blob([text], { type: mime + ";charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
  }

  function saveMessage(msg, ext) {
    const id = msg.dataset.msgId || String(Date.now());
    const mime = ext === "md" ? "text/markdown" : "text/plain";
    download("chat-" + currentChatId() + "-" + id + "." + ext, getSource(msg), mime);
  }

  function saveWholeChat() {
    const title = $(".chat-title").textContent.trim();
    const parts = ["# " + title, ""];
    document.querySelectorAll("#messages .msg").forEach(function (msg) {
      if (msg.dataset.role === "assistant") {
        const model = msg.dataset.model || "";
        parts.push("## Ассистент" + (model ? " (" + model + ")" : ""), "");
      } else {
        parts.push("## Вы", "");
      }
      parts.push(getSource(msg), "");
    });
    download("chat-" + currentChatId() + ".md", parts.join("\n"), "text/markdown");
  }

  // ---------------------------------------------------------------------------------------
  // 3. Page state and DOM building
  // ---------------------------------------------------------------------------------------

  const chatEl = document.getElementById("chat");
  const messagesEl = document.getElementById("messages");
  const composer = document.getElementById("composer");

  function currentChatId() {
    return chatEl ? chatEl.dataset.chatIdCurrent : "";
  }

  /** New message element from <template id="tpl-user|tpl-assistant">. */
  function newMessage(role, text, fileNames) {
    const msg = $("#tpl-" + role).content.firstElementChild.cloneNode(true);
    setSource(msg, text);
    if (fileNames && fileNames.length) {
      const chips = document.createElement("ul");
      chips.className = "chips";
      fileNames.forEach(function (name) {
        const li = document.createElement("li");
        li.className = "chip";
        li.textContent = name;
        chips.appendChild(li);
      });
      msg.insertBefore(chips, msg.firstChild);
    }
    const hint = $(".empty-hint", messagesEl);
    if (hint) hint.hidden = true;
    messagesEl.appendChild(msg);
    renderMessage(msg);
    msg.scrollIntoView({ block: "end" });
    return msg;
  }

  function setMeta(msg, text) {
    $(".meta-info", msg).textContent = text;
  }

  function formatDone(data) {
    const tokens = (data.prompt_tokens || 0) + (data.completion_tokens || 0);
    const cost = data.cost_usd === null || data.cost_usd === undefined
      ? "$?"
      : "$" + Number(data.cost_usd).toFixed(4);
    return data.model + " · " + tokens + " ток. · " + cost;
  }

  function showError(msg, text, canRetry) {
    const banner = $("#tpl-error").content.firstElementChild.cloneNode(true);
    $(".error-text", banner).textContent = text;
    const retryButton = $(".retry", banner);
    if (canRetry) {
      retryButton.addEventListener("click", function () { retry(msg); });
    } else {
      retryButton.remove();
    }
    $(".meta", msg).hidden = true;
    msg.appendChild(banner);
    msg.scrollIntoView({ block: "end" });
  }

  /** Reload the sidebar a bit later: the generated title and new order show up. */
  function refreshSidebar() {
    setTimeout(function () {
      htmx.ajax("GET", "/app/chats/sidebar?current=" + currentChatId(), {
        target: "#chat-list",
        swap: "outerHTML",
      }).then(function () {
        // Copy the (maybe new) title of the current chat into the header and the tab.
        const link = $("#chat-list .chat-item.current .chat-link");
        if (link) {
          $(".chat-title").textContent = link.textContent;
          document.title = link.textContent;
        }
      });
    }, 3000);
  }

  // ---------------------------------------------------------------------------------------
  // 4. Talking to the server
  // ---------------------------------------------------------------------------------------

  let controller = null; // AbortController of the running request, if any

  function setBusy(busy) {
    $("#send").hidden = busy;
    $("#stop").hidden = !busy;
    $("#input").disabled = busy;
  }

  /** On the empty "Новый чат" screen the chat is created on the first send. */
  async function ensureChat() {
    if (currentChatId()) return currentChatId();
    const response = await fetch("/app/chats", {
      method: "POST",
      headers: { "X-CSRF-Token": csrfToken(), "HX-Request": "true" },
      credentials: "same-origin",
    });
    const location = response.headers.get("HX-Redirect") || "";
    const match = location.match(/^\/app\/chats\/(\d+)$/);
    if (!match) throw new Error("chat not created");
    chatEl.dataset.chatIdCurrent = match[1];
    history.replaceState(null, "", location);
    $("#save-chat").hidden = false;
    return match[1];
  }

  /** Friendly Russian text for an answer that is not an event stream (plain HTTP error). */
  async function httpErrorMessage(response) {
    if (response.status === 413) return MSG_TOO_LARGE;
    if (response.status === 403) return "Сессия устарела — обновите страницу";
    if (response.status === 404) return "Чат не найден — обновите страницу";
    if (response.status === 415) return "Неподдерживаемый формат запроса — обновите страницу";
    try {
      // FastAPI errors look like {"detail": "..."}; show it only when it is our Russian text.
      const data = await response.json();
      if (data && typeof data.detail === "string" && /[А-Яа-яЁё]/.test(data.detail)) {
        return data.detail;
      }
    } catch (e) {
      // not JSON — fall through
    }
    return MSG_GENERIC;
  }

  /** Split an SSE block ("event: x\ndata: {...}") into {event, data}. */
  function parseEvent(block) {
    let event = "message";
    const data = [];
    block.split("\n").forEach(function (line) {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
    });
    if (!data.length) return null;
    try {
      return { event: event, data: JSON.parse(data.join("\n")) };
    } catch (e) {
      return null;
    }
  }

  /**
   * POST to `url` and stream the answer into the assistant message `msg`.
   * `isRetry` keeps the "Повторить" button on errors of a retry request.
   */
  async function stream(url, body, msg, isRetry) {
    controller = new AbortController();
    setBusy(true);
    let text = "";
    let finished = false; // got `done` or `error`
    const renderer = throttledRenderer(msg);

    function handle(ev) {
      if (ev.event === "delta") {
        text += ev.data.t;
        setSource(msg, text);
        renderer.schedule();
      } else if (ev.event === "done") {
        finished = true;
        renderer.flush();
        msg.dataset.msgId = ev.data.message_id || "";
        msg.dataset.model = ev.data.model || "";
        setMeta(msg, formatDone(ev.data));
        $("#spend").textContent = "Расход: $" + ev.data.spend + " / $" + ev.data.limit;
        refreshSidebar();
      } else if (ev.event === "error") {
        finished = true;
        renderer.flush();
        showError(msg, ev.data.message, ev.data.retry === true || isRetry);
      }
    }

    try {
      const response = await fetch(url, {
        method: "POST",
        body: body,
        headers: { "X-CSRF-Token": csrfToken() },
        credentials: "same-origin",
        signal: controller.signal,
      });
      const type = response.headers.get("Content-Type") || "";
      if (!response.ok || !type.startsWith("text/event-stream")) {
        showError(msg, await httpErrorMessage(response), isRetry);
        return;
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const chunk = await reader.read();
        if (chunk.done) break;
        buffer += decoder.decode(chunk.value, { stream: true });
        // Events are separated by an empty line (our server uses LF line ends only);
        // a partial event stays in the buffer until the rest arrives.
        let end;
        while ((end = buffer.indexOf("\n\n")) !== -1) {
          const ev = parseEvent(buffer.slice(0, end));
          buffer = buffer.slice(end + 2);
          if (ev) handle(ev);
        }
      }
      if (!finished) {
        renderer.flush();
        showError(msg, MSG_BROKEN, true);
      }
    } catch (err) {
      renderer.flush();
      if (err.name === "AbortError") {
        // "Стоп": the server keeps what was received and marks it as interrupted.
        setMeta(msg, "прервано");
      } else if (!finished) {
        showError(msg, text ? MSG_BROKEN : MSG_NETWORK, text !== "" || isRetry);
      }
    } finally {
      controller = null;
      setBusy(false);
      $("#input").focus();
    }
  }

  /** "Повторить": drop the failed answer and ask the server to answer the last question again. */
  function retry(failedMsg) {
    if (controller) return;
    failedMsg.remove();
    const msg = newMessage("assistant", "");
    const model = encodeURIComponent($("#model").value);
    stream("/app/chats/" + currentChatId() + "/retry?model=" + model, null, msg, true);
  }

  // ---------------------------------------------------------------------------------------
  // 5. Composer
  // ---------------------------------------------------------------------------------------

  let files = []; // File objects waiting to be sent

  function drawChips() {
    const list = $("#file-chips");
    list.textContent = "";
    files.forEach(function (file, index) {
      const li = document.createElement("li");
      li.className = "chip";
      li.textContent = file.name + " ";
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "chip-remove";
      remove.title = "Убрать файл";
      remove.textContent = "✕";
      remove.addEventListener("click", function () {
        files.splice(index, 1);
        drawChips();
      });
      li.appendChild(remove);
      list.appendChild(li);
    });
  }

  function autoGrow(textarea) {
    textarea.style.height = "auto"; // CSSOM, allowed by the CSP (unlike style="" in markup)
    textarea.style.height = Math.min(textarea.scrollHeight, 240) + "px";
  }

  async function send() {
    const input = $("#input");
    const text = input.value;
    if (controller || (!text.trim() && files.length === 0)) return;

    const sending = files;
    const form = new FormData();
    form.append("text", text);
    form.append("model", $("#model").value);
    sending.forEach(function (file) { form.append("files", file, file.name); });

    let chatId;
    try {
      chatId = await ensureChat();
    } catch (e) {
      const msg = newMessage("assistant", "");
      showError(msg, MSG_NETWORK, false);
      return;
    }

    newMessage("user", text, sending.map(function (f) { return f.name; }));
    input.value = "";
    autoGrow(input);
    files = [];
    drawChips();

    const msg = newMessage("assistant", "");
    await stream("/app/chats/" + chatId + "/messages", form, msg, false);
    if (!input.value && $(".error-banner", msg) && !$(".retry", msg)) {
      // The server did not keep the question (limits, bad file…): give it back for editing.
      input.value = text;
      autoGrow(input);
      files = sending;
      drawChips();
    }
  }

  function initComposer() {
    const input = $("#input");
    input.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
        e.preventDefault();
        send();
      }
    });
    input.addEventListener("input", function () { autoGrow(input); });
    composer.addEventListener("submit", function (e) {
      e.preventDefault();
      send();
    });
    $("#stop").addEventListener("click", function () {
      if (controller) controller.abort();
    });
    $("#file-input").addEventListener("change", function (e) {
      files = files.concat(Array.from(e.target.files));
      e.target.value = ""; // allow picking the same file again
      drawChips();
    });
    messagesEl.addEventListener("click", function (e) {
      const button = e.target.closest("button.save");
      if (button) saveMessage(button.closest(".msg"), button.dataset.ext);
    });
    $("#save-chat").addEventListener("click", saveWholeChat);
    input.focus();
  }

  // ---------------------------------------------------------------------------------------
  // 6. Start
  // ---------------------------------------------------------------------------------------

  document.querySelectorAll("#messages .msg").forEach(function (msg) {
    if (msg.dataset.role === "assistant") {
      msg.dataset.model = $(".meta-info", msg).textContent.split(" · ")[0];
    }
    renderMessage(msg);
  });
  if (composer) initComposer();
  if (messagesEl && messagesEl.lastElementChild) messagesEl.lastElementChild.scrollIntoView({ block: "end" });
})();
