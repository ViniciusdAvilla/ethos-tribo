'use strict';
(() => {
  let toastTimer;
  function flash(message) {
    const toast = document.getElementById('toast');
    if (!toast) return;
    toast.textContent = message;
    toast.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toast.classList.remove('show'), 2600);
  }
  async function copyText(value) {
    try {
      await navigator.clipboard.writeText(value);
    } catch (_) {
      // Browsers without Clipboard API can use a short-lived selection fallback.
      const temp = document.createElement('textarea');
      temp.value = value;
      temp.style.cssText = 'position:fixed;left:-9999px;top:-9999px';
      document.body.appendChild(temp);
      temp.select();
      const copied = document.execCommand('copy');
      temp.remove();
      if (!copied) { flash('Selecione e copie o texto manualmente.'); return; }
    }
    flash('Copiado!');
  }
  document.addEventListener('click', (event) => {
    const control = event.target.closest('[data-copy-target],[data-copy-text]');
    if (!control) return;
    const text = control.dataset.copyText || document.getElementById(control.dataset.copyTarget)?.value || '';
    if (text) copyText(text);
  });
  document.querySelectorAll('form[data-confirm]').forEach((form) => {
    form.addEventListener('submit', (event) => {
      if (!window.confirm(form.dataset.confirm)) event.preventDefault();
    });
  });
})();
// Poll only the authenticated owner's order; a server-verified status triggers completion.
(() => {
  const watch = document.querySelector('[data-payment-status]');
  if (!watch) return;
  let attempts = 0;
  async function verify() {
    if (document.hidden) { setTimeout(verify, 10000); return; }
    try {
      const response = await fetch(watch.dataset.paymentStatus, {credentials:'same-origin',cache:'no-store'});
      if (response.ok) {
        const status = await response.json();
        if (status.paid || status.cancelled) { location.reload(); return; }
      }
    } catch (_) {}
    attempts += 1;
    if (attempts < 60) setTimeout(verify, 10000);
    else watch.textContent = 'Pagamento ainda pendente. Use Verificar pagamento para consultar novamente.';
  }
  setTimeout(verify, 10000);
})();
