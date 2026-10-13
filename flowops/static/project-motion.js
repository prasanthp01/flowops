/** Presentation only. Content and demo controls remain usable without motion. */
export function initProjectMotion(doc = document, view = window) {
  if (!doc.body.classList.contains('case-page')) return () => {};
  const preference = view.matchMedia('(prefers-reduced-motion: reduce)');
  if (preference.matches || !view.IntersectionObserver) return () => {};
  const running = new Map();
  let stopped = false;
  const animate = (element, delay = 0) => {
    if (stopped || !element.animate || element.contains(doc.activeElement)) return;
    const animation = element.animate([
      { opacity: .15, transform: 'translate3d(0, 14px, 0)' },
      { opacity: 1, transform: 'translate3d(0, 0, 0)' },
    ], { duration: 560, delay, easing: 'cubic-bezier(.16, 1, .3, 1)', fill: 'backwards' });
    running.set(element, animation);
    animation.finished.then(() => {
      if (running.get(element) === animation) running.delete(element);
    }).catch(() => {});
  };
  // Deep links and restored scroll positions must land immediately in readable content.
  if (!view.location.hash && view.scrollY < 24) {
    doc.querySelectorAll('.hero > .hero-copy, .project-hero > .case-hero-copy, .hero-diagram, .hero-visual, .hero-illustration, .case-hero-visual')
      .forEach((element, index) => animate(element, Math.min(index, 1) * 90));
  }
  const observer = new view.IntersectionObserver(entries => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      observer.unobserve(entry.target);
      animate(entry.target);
    }
  }, { threshold: .08, rootMargin: '0px 0px -24px 0px' });
  // Only editorial groups: never translate fields, results, trace events or tabs.
  const selectors = [
    'main > section:not(.hero):not(.project-hero) > h2',
    'main > section > .section-heading',
    'main > section > .section-head',
    '.architecture-cards', '.architecture-flow',
    '.evidence-grid', '.case-footer-top',
  ];
  doc.querySelectorAll(selectors.join(',')).forEach(element => {
    if (element.closest('#simulation, #try-it, form, resolveai-studio')) return;
    if (element.getBoundingClientRect().top >= view.innerHeight) observer.observe(element);
  });
  const cancelFocused = event => {
    for (const [element, animation] of running) {
      if (element.contains(event.target)) {
        animation.cancel();
        running.delete(element);
      }
    }
  };
  const finish = () => {
    stopped = true;
    observer.disconnect();
    running.forEach(animation => animation.cancel());
    running.clear();
  };
  const onPreference = () => { if (preference.matches) finish(); };
  const onRestore = event => { if (event.persisted) finish(); };
  doc.addEventListener('focusin', cancelFocused);
  preference.addEventListener('change', onPreference);
  view.addEventListener('pageshow', onRestore);
  return () => {
    finish();
    doc.removeEventListener('focusin', cancelFocused);
    preference.removeEventListener('change', onPreference);
    view.removeEventListener('pageshow', onRestore);
  };
}

if (typeof document !== 'undefined') initProjectMotion();
