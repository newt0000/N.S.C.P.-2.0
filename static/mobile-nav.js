document.addEventListener('DOMContentLoaded', () => {
  const button = document.getElementById('mobile-menu-toggle');
  const sidebar = document.getElementById('nscp-sidebar');
  const backdrop = document.getElementById('mobile-nav-backdrop');
  if (!button || !sidebar || !backdrop) return;
  const setOpen = (open) => {
    sidebar.classList.toggle('mobile-nav-open', open);
    document.body.classList.toggle('mobile-nav-active', open);
    backdrop.hidden = !open;
    button.setAttribute('aria-expanded', String(open));
    button.setAttribute('aria-label', open ? 'Close navigation menu' : 'Open navigation menu');
    if (!open) button.focus({preventScroll:true});
  };
  button.addEventListener('click', () => setOpen(!sidebar.classList.contains('mobile-nav-open')));
  backdrop.addEventListener('click', () => setOpen(false));
  sidebar.querySelectorAll('nav a').forEach(link => link.addEventListener('click', () => setOpen(false)));
  document.addEventListener('keydown', (event) => { if (event.key === 'Escape' && sidebar.classList.contains('mobile-nav-open')) setOpen(false); });
  window.matchMedia('(min-width:901px)').addEventListener('change', (event) => { if (event.matches && sidebar.classList.contains('mobile-nav-open')) setOpen(false); });
});
