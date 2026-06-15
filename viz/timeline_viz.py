"""Frise chronologique interactive (Plotly) à partir du registre documents.

Un point par document (mail ou PJ), axe X = date réelle, couleur = affaire,
symbole = type. Hover = correspondant + sujet/PJ. Le clic est géré côté JS
(customdata = doc_id / email_id) pour ouvrir le document.
Retourne un fragment HTML (div) à embarquer dans timeline.html.
"""
from __future__ import annotations

from datetime import datetime

import plotly.graph_objects as go

_AFFAIRE_COLORS = {
    "succession": "#2c7fb8",
    "dette": "#d95f0e",
    "pro": "#756bb1",
    "autre": "#969696",
}
_AFFAIRES = ["succession", "dette", "pro", "autre"]


def _parse_date(iso: str) -> datetime | None:
    try:
        return datetime.fromisoformat(iso)
    except Exception:
        return None


def build_timeline_html(documents: list[dict]) -> str:
    """documents : lignes du registre `documents` (state.list_documents())."""
    if not documents:
        return "<p>Aucun document indexé. Lancez l'indexation depuis <a href='/admin'>/admin</a>.</p>"

    fig = go.Figure()

    for affaire in _AFFAIRES:
        xs, ys, texts, custom = [], [], [], []
        for d in documents:
            if d.get("affaire") != affaire:
                continue
            dt = _parse_date(d.get("date", ""))
            if dt is None:
                continue
            is_mail = d.get("type") == "mail"
            xs.append(dt)
            # jitter vertical léger : mails en haut, PJ en bas
            ys.append(1 if is_mail else 0)
            kind = "✉ Mail" if is_mail else "📎 PJ"
            texts.append(
                f"{kind}<br>{d.get('date','')[:10]} — {d.get('correspondent','')}"
                f"<br>{(d.get('subject') or '')[:80]}"
            )
            custom.append({"doc_id": d.get("doc_id"), "email_id": d.get("email_id")})
        if not xs:
            continue
        fig.add_trace(
            go.Scatter(
                x=xs,
                y=ys,
                mode="markers",
                name=affaire,
                marker=dict(
                    size=9,
                    color=_AFFAIRE_COLORS.get(affaire, "#969696"),
                    symbol=["circle" if y == 1 else "diamond" for y in ys],
                    line=dict(width=0.5, color="white"),
                ),
                text=texts,
                hovertemplate="%{text}<extra></extra>",
                customdata=custom,
            )
        )

    fig.update_layout(
        height=520,
        margin=dict(l=20, r=20, t=30, b=40),
        yaxis=dict(
            tickmode="array",
            tickvals=[0, 1],
            ticktext=["Pièces jointes", "Mails"],
            range=[-0.5, 1.5],
        ),
        xaxis=dict(title="Date réelle d'envoi"),
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
        hovermode="closest",
        plot_bgcolor="#fafafa",
    )

    return fig.to_html(
        full_html=False,
        include_plotlyjs="cdn",
        div_id="timeline-plot",
        config={"displayModeBar": True, "responsive": True},
    )
