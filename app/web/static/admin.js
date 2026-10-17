(() => {
  const btn = document.getElementById("copy-invite");
  const field = document.getElementById("invite-url");
  if (!btn || !field) return;
  btn.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(field.value);
      btn.textContent = "Скопировано";
    } catch {
      field.select();
      btn.textContent = "Выделено — нажмите Ctrl+C";
    }
  });
})();
