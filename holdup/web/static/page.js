// Small enhancements for the HoldUp pages: live countdowns, copy buttons and hold refresh.
(() => {
  "use strict";

  function duration(ms) {
    let seconds = Math.max(0, Math.round(ms / 1000));
    if (seconds < 60) return `${seconds} s`;
    let minutes = Math.floor(seconds / 60);
    seconds %= 60;
    if (minutes < 60) return `${minutes} min ${String(seconds).padStart(2, "0")} s`;
    let hours = Math.floor(minutes / 60);
    minutes %= 60;
    if (hours < 48) return minutes ? `${hours} h ${minutes} min` : `${hours} h`;
    return `${Math.floor(hours / 24)} days`;
  }

  // Countdowns use the server's clock, so a skewed device clock does not matter.
  document.querySelectorAll("[data-countdown]").forEach((element) => {
    const target = Number(element.dataset.targetMs);
    const offset = Number(element.dataset.nowMs) - Date.now();
    let timer = 0;
    const tick = () => {
      const left = target - (Date.now() + offset);
      if (left > 0) {
        element.textContent = `in ${duration(left)}`;
      } else {
        element.textContent = "due now: the keeper releases it shortly";
        clearInterval(timer);
      }
    };
    timer = setInterval(tick, 1000);
    tick();
  });

  document.querySelectorAll("[data-copy]").forEach((button) => {
    button.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(button.dataset.copy);
        button.textContent = "Copied";
      } catch (_) {
        button.textContent = "Select to copy";
      }
      setTimeout(() => {
        button.textContent = "Copy";
      }, 1600);
    });
  });

  // A hold page reloads when its hold changes: a brake, a cancel or the release.
  const hold = document.querySelector("[data-hold-poll][data-active]");
  if (hold) {
    const id = hold.dataset.holdPoll;
    const signature = hold.dataset.signature;
    const poll = async () => {
      if (document.visibilityState === "visible") {
        try {
          const response = await fetch(`/h/${id}.json`, { cache: "no-store" });
          if (response.ok) {
            const data = await response.json();
            if (data.signature !== signature) {
              window.location.reload();
              return;
            }
          }
        } catch (_) {
          // offline for a moment; try again on the next tick
        }
      }
      setTimeout(poll, 4000);
    };
    setTimeout(poll, 4000);
  }
})();
