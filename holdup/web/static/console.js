// The HoldUp web console: a chat over the same engine as the Telegram bot.
(() => {
  "use strict";

  const root = document.querySelector("[data-console]");
  if (!root) return;

  const find = (selector) => root.querySelector(selector);
  const list = find("[data-messages]");
  const chat = find("[data-chat]");
  const welcome = find("[data-welcome]");
  const input = find("[data-input]");
  const submit = find("[data-submit]");
  const composer = find("[data-composer]");
  const chips = root.querySelectorAll("[data-chips] .chip");
  const accountBox = find("[data-account]");
  const startButton = find("[data-start]");
  const nameInput = find("[data-name]");
  const startError = find("[data-start-error]");
  const invite = root.dataset.invite || "";

  const ONE_SHOT = ["confirm:", "drop:", "sample:"];
  const URL_PATTERN = /https?:\/\/[^\s<>"']+/g;
  let seq = 0;
  let started = false;
  let busy = false;
  let pollTimer = 0;
  let lastAccountRefresh = 0;

  async function api(path, body) {
    const options =
      body === undefined
        ? { cache: "no-store" }
        : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
    const response = await fetch(path, { credentials: "same-origin", ...options });
    let data = null;
    try {
      data = await response.json();
    } catch (_) {
      data = null;
    }
    if (!response.ok) {
      const error = new Error((data && (data.error || data.detail)) || `request failed (${response.status})`);
      error.status = response.status;
      throw error;
    }
    return data;
  }

  // Message text is shown as text; only URLs become links.
  function renderText(node, text) {
    let last = 0;
    for (const match of text.matchAll(URL_PATTERN)) {
      const url = match[0].replace(/[.,;:!?)]+$/, "");
      node.append(text.slice(last, match.index));
      const link = document.createElement("a");
      link.href = url;
      link.textContent = url;
      link.rel = "noopener noreferrer";
      link.target = "_blank";
      node.append(link);
      last = match.index + url.length;
    }
    node.append(text.slice(last));
  }

  function clock(ms) {
    return new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }

  function addMessage(message) {
    if (message.seq <= seq) return;
    seq = message.seq;
    const item = document.createElement("li");
    item.className = `msg msg-${message.role}` + (message.tone ? ` tone-${message.tone}` : "");
    if (message.title) {
      const title = document.createElement("span");
      title.className = "msg-title";
      title.textContent = message.title;
      item.append(title);
    }
    const body = document.createElement("span");
    renderText(body, message.text);
    item.append(body);
    const buttons = (message.buttons || []).flat();
    if (buttons.length) {
      const row = document.createElement("div");
      row.className = "msg-buttons";
      for (const spec of buttons) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "msg-button";
        button.textContent = spec.label;
        button.addEventListener("click", () => {
          if (busy) return;
          if (ONE_SHOT.some((prefix) => spec.data.startsWith(prefix))) {
            row.querySelectorAll("button").forEach((other) => {
              other.disabled = true;
            });
          }
          act(spec.data, spec.label);
        });
        row.append(button);
      }
      item.append(row);
    }
    const time = document.createElement("span");
    time.className = "msg-time";
    time.textContent = clock(message.ts);
    item.append(time);
    list.append(item);
  }

  function note(text) {
    const item = document.createElement("li");
    item.className = "typing";
    item.textContent = text;
    list.append(item);
    scrollDown();
  }

  function scrollDown() {
    chat.scrollTop = chat.scrollHeight;
  }

  function showAccount(account) {
    accountBox.hidden = false;
    find("[data-account-id]").textContent = account.id;
    find("[data-balances]").textContent = account.balances.join(" \u00b7 ");
  }

  function apply(data) {
    if (!data) return 0;
    const messages = data.messages || [];
    messages.forEach(addMessage);
    if (data.account) showAccount(data.account);
    if (messages.length) scrollDown();
    return messages.length;
  }

  function setStarted(value) {
    started = value;
    welcome.hidden = value;
    input.disabled = !value;
    submit.disabled = !value;
    chips.forEach((chip) => {
      chip.disabled = !value;
    });
    if (value) schedulePoll();
  }

  async function run(work) {
    if (busy) return;
    busy = true;
    submit.disabled = true;
    const typing = document.createElement("li");
    typing.className = "typing";
    typing.textContent = "HoldUp is working\u2026";
    list.append(typing);
    scrollDown();
    try {
      apply(await work());
    } catch (error) {
      if (error.status === 401) {
        setStarted(false);
      } else {
        note(`Could not send: ${error.message}`);
      }
    } finally {
      typing.remove();
      busy = false;
      submit.disabled = !started;
      if (started) input.focus();
    }
  }

  const send = (text) => run(() => api("/console/api/send", { text, after: seq }));
  const act = (data, label) => run(() => api("/console/api/action", { data, label, after: seq }));

  function schedulePoll() {
    clearTimeout(pollTimer);
    pollTimer = setTimeout(poll, 2000);
  }

  async function poll() {
    if (started && !busy && document.visibilityState === "visible") {
      try {
        const fresh = apply(await api(`/console/api/messages?after=${seq}`));
        if (fresh && Date.now() - lastAccountRefresh > 4000) {
          lastAccountRefresh = Date.now();
          apply(await api(`/console/api/state?after=${seq}`));
        }
      } catch (_) {
        // the server may be restarting; keep polling
      }
    }
    schedulePoll();
  }

  function autosize() {
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 160)}px`;
  }

  startButton.addEventListener("click", async () => {
    startButton.disabled = true;
    startError.hidden = true;
    try {
      const data = await api("/console/api/start", { name: nameInput.value.trim(), invite, after: seq });
      setStarted(true);
      apply(data);
      if (invite) window.history.replaceState(null, "", "/console");
    } catch (error) {
      startError.textContent = error.message;
      startError.hidden = false;
    } finally {
      startButton.disabled = false;
    }
  });

  composer.addEventListener("submit", (event) => {
    event.preventDefault();
    const text = input.value.trim();
    if (!text || busy || !started) return;
    input.value = "";
    autosize();
    send(text);
  });

  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      composer.requestSubmit();
    }
  });
  input.addEventListener("input", autosize);

  chips.forEach((chip) => {
    chip.addEventListener("click", () => {
      if (chip.dataset.send) send(chip.dataset.send);
      else act(chip.dataset.action, chip.dataset.label);
    });
  });

  api("/console/api/state?after=0")
    .then((data) => {
      setStarted(Boolean(data && data.started));
      if (!started) return;
      if (!apply(data)) note("Welcome back. Type /help or tap a quick action below.");
      if (invite) {
        window.history.replaceState(null, "", "/console");
        send(`/start g_${invite}`);
      }
    })
    .catch(() => setStarted(false));
})();
