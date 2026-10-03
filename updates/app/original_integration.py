"""Server-rendered additions to the original Ethos Tribo HTML export.

The uploaded RAR is an HTTrack snapshot, not the Next.js application source.
Only inject the upcoming events block immediately before the original
'O que é a Ethos Tribo' section; leave original imagery and section layout intact.
"""
from __future__ import annotations

from datetime import date
from html import escape
from pathlib import Path
import re
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.orm import Session
from .models import Event
from .services import occupancy, money

SITE = Path(__file__).resolve().parent / "original_site"
MAIN_PAGE = SITE / "index.html"
INJECTION_ANCHOR = '<div style="max-width:var(--layout-content-max);margin:0 auto;padding:var(--space-4xl) var(--space-xl)"><div style="text-align:center"><span style="font-family:var(--font-family-caption);font-size:var(--font-size-caption);font-weight:var(--font-weight-caption);line-height:var(--line-height-caption);margin:0;color:var(--color-action)">O que é a Ethos Tribo</span>'


def upcoming_events(db: Session) -> list[Event]:
    return list(db.scalars(select(Event).where(
        Event.is_published.is_(True), Event.event_date >= date.today()
    ).order_by(Event.event_date.asc(), Event.starts_at.asc())).all())


def upcoming_section(db: Session, member_logged_in: bool) -> str:
    events = upcoming_events(db)
    cards = []
    for event in events:
        confirmed, waiting = occupancy(db, event.id)
        spaces = max(event.capacity - confirmed, 0)
        pill = 'Vagas abertas' if spaces and not event.is_closed else ('Lista de espera' if not event.is_closed else 'Inscrições encerradas')
        title = escape(event.title)
        d = event.event_date.strftime('%d/%m/%Y')
        start, end = event.starts_at.strftime('%Hh%M'), event.ends_at.strftime('%Hh%M')
        # The original site's designs intentionally use green/lime editorial accents.
        cards.append(f'''<article class="integrated-event-card">
          <div class="integrated-cover" aria-hidden="true"><span>ETHOS · ENCONTRO PRESENCIAL</span><span class="integrated-cover-orb">✳</span></div>
          <div class="integrated-event-info">
            <div class="integrated-event-meta"><span>{escape(str(event.court_count))} quadra{'s' if event.court_count != 1 else ''}</span><strong>{escape(money(event.price_cents))}</strong></div>
            <h3>{title}</h3><p>{escape(event.description or 'Encontre sua tribo e viva essa experiência fora da tela.')}</p>
            <div class="integrated-event-facts"><span>◷ &nbsp;{d} · {start} às {end}</span><span>⌖ &nbsp;{escape(event.location_name)}</span></div>
            <div class="integrated-capacity"><span><b>{confirmed}</b> / {event.capacity} participantes</span><span>{escape(pill)}</span></div>
            <progress max="{event.capacity}" value="{min(confirmed,event.capacity)}" aria-label="Vagas ocupadas">{confirmed}/{event.capacity}</progress>
            <a class="integrated-event-link" href="/evento/{quote(event.slug)}">Ver encontro <span aria-hidden="true">↗</span></a>
          </div>
        </article>''')
    items = '<div class="integrated-grid">' + ''.join(cards) + '</div>' if cards else '<div class="integrated-empty"><h3>Novas experiências vêm aí</h3><p>Acompanhe esta página para descobrir os próximos encontros.</p></div>'
    # This is rendered ONLY on the server when a valid member DB session exists.
    presence = '''<aside class="integrated-presence" aria-label="Área exclusiva do membro">
        <div><span>EXCLUSIVO PARA MEMBROS</span><h3>PRESENÇA<br>EM JOGO<span class="lime">.</span></h3><p>Você está conectado. Sua próxima experiência começa aqui.</p></div>
        <a href="/conta" class="integrated-presence-btn">Minha conta ↗</a>
    </aside>''' if member_logged_in else ''
    return f'''<section id="proximos-encontros" class="integrated-upcoming" aria-labelledby="upcoming-heading">
      <div class="integrated-container"><div class="integrated-heading">
        <div><span class="integrated-kicker">AGENDA ETHOS</span><h2 id="upcoming-heading">Próximos encontros<span class="lime">.</span></h2><p>Escolha sua próxima experiência. O resto acontece ao vivo.</p></div>
        <span class="integrated-total">{len(events)} encontro{'s' if len(events) != 1 else ''}</span>
      </div>{presence}{items}</div>
    </section>'''


def make_original_html(db: Session, is_member: bool, *, page: str = 'index.html') -> str:
    path = SITE / page
    html = path.read_text(encoding='utf-8', errors='replace')
    if page == 'index.html':
        if html.count(INJECTION_ANCHOR) != 1:
            raise RuntimeError('O marco de inserção não foi encontrado no HTML original; revise antes de atualizar o site.')
        html = html.replace(INJECTION_ANCHOR, upcoming_section(db, is_member) + INJECTION_ANCHOR, 1)
        html = html.replace('</head>', '<link rel="stylesheet" href="/integration/ethos-upcoming.css" /></head>', 1)
    # Next hydration scripts from the archived static export may replace HTML injected
    # by the server. Preserve HTML and CSS but disable obsolete snapshot runtime scripts.
    # The cached build preloads many obsolete JS chunks, none used by the static pages.
    html = re.sub(r'<link\b(?=[^>]*\brel="preload")(?=[^>]*\bas="script")[^>]*>', '', html)
    html = re.sub(r'<script\b[^>]*\bsrc="(?:/?_next/)[^"]*"[^>]*></script>', '', html)
    html = re.sub(r'<script\b[^>]*>\s*(?:self\.__next_s|self\.__next_f|\(self\.__next_s)[\s\S]*?</script>', '', html)
    html = re.sub(r'<script\b[^>]*src="https://static\.cloudflareinsights\.com/[^>]*></script>', '', html)
    # Actual member auth is handled by this server. Original exported account
    # pages are a nonfunctional snapshot and must not be offered as a live login.
    html = html.replace('href="entrar.html"', 'href="/conta"' if is_member else 'href="/conta/entrar"')
    html = html.replace('href="/entrar"', 'href="/conta"' if is_member else 'href="/conta/entrar"')
    if is_member:
        # Change only the top navigation call-to-action while preserving markup.
        html = re.sub(r'(<a\b[^>]*href="/conta"[^>]*>)Entrar(</a>)', r'\1Minha conta\2', html, count=1)
        html = re.sub(r'(<a\b[^>]*href="/conta"[^>]*>)Entrar na Tribo(</a>)', r'\1Minha conta\2', html)
    # Lead the old agenda link to the new live, server-backed agenda.
    html = html.replace('href="agenda.html"', 'href="/#proximos-encontros"')
    # Archived filter pages lead to the live agenda; event creation requires admin auth.
    html = re.sub(r'href="(?:/?encontros/curitiba-pr\.html|/?agenda(?:fe6f|5234|bc0f)\.html(?:\?[^"]*)?)"', 'href="/#proximos-encontros"', html)
    html = html.replace('href="encontro/novo.html"', 'href="/admin/eventos/novo"')
    if page == 'index.html':
        html = html.replace('</head>', '<meta name="robots" content="index,follow" /></head>', 1)
    from .media import apply_media_html
    return apply_media_html(db, html)
