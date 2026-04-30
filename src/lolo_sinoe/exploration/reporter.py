"""Generate REPORT.md from crawl results."""

from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from lolo_sinoe.exploration.crawler import CrawlResult


def generate_report(result: CrawlResult, *, output_path: Path) -> Path:
    """Render a markdown report summarizing the crawl."""
    lines: list[str] = []
    lines.append("# SINOE — Reporte de exploración")
    lines.append("")
    lines.append(f"**Generado:** {datetime.now(UTC).isoformat()}")
    lines.append(f"**Páginas visitadas:** {len(result.visited)}")
    lines.append(f"**URLs salteadas:** {len(result.skipped)}")
    lines.append("")

    lines.append("## 1. Mapa del sitio")
    lines.append("")
    by_depth: dict[int, list[str]] = {}
    for v in result.visited:
        by_depth.setdefault(v.depth, []).append(f"- `{v.url}` — {v.title or '(sin título)'}")
    for depth in sorted(by_depth):
        lines.append(f"### Profundidad {depth}")
        lines.extend(by_depth[depth])
        lines.append("")

    lines.append("## 2. Inventario de funcionalidades observadas")
    lines.append("")
    lines.append("| URL | Título | Forms | Links | Tablas | Descargables |")
    lines.append("|---|---|---|---|---|---|")
    for v in result.visited:
        lines.append(
            f"| `{v.url[:80]}` | {v.title[:60]} | {len(v.forms_found)} | "
            f"{len(v.links_found)} | {len(v.tables_found)} | {len(v.downloadables_found)} |"
        )
    lines.append("")

    lines.append("## 3. Endpoints de formularios detectados")
    lines.append("")
    form_actions: Counter[str] = Counter()
    for v in result.visited:
        for f in v.forms_found:
            form_actions[f"{f.method} {f.action}"] += 1
    if form_actions:
        lines.append("| Método/URL | Veces |")
        lines.append("|---|---|")
        for action, n in form_actions.most_common():
            lines.append(f"| `{action[:120]}` | {n} |")
    else:
        lines.append("_No se detectaron forms._")
    lines.append("")

    lines.append("## 4. Archivos descargables (metadata, NO descargados)")
    lines.append("")
    lines.append("| Página | URL del archivo | Tipo | Texto del link |")
    lines.append("|---|---|---|---|")
    for v in result.visited:
        for d in v.downloadables_found:
            lines.append(
                f"| `{v.url[:60]}` | `{d.url[:80]}` | {d.inferred_kind} | {d.text[:60]} |"
            )
    lines.append("")

    lines.append("## 5. Componentes JSF detectados (top 30 por página)")
    lines.append("")
    for v in result.visited:
        if not v.jsf_components:
            continue
        lines.append(f"### `{v.url[:80]}`")
        for cid in v.jsf_components[:30]:
            lines.append(f"- `{cid}`")
        lines.append("")

    lines.append("## 6. URLs salteadas y razón")
    lines.append("")
    if result.skipped:
        lines.append("| URL | Razón |")
        lines.append("|---|---|")
        for url, reason in result.skipped[:200]:
            lines.append(f"| `{url[:80]}` | {reason[:100]} |")
    else:
        lines.append("_Ninguna._")
    lines.append("")

    lines.append("## 7. Oportunidades de implementación (a completar manualmente)")
    lines.append("")
    lines.append(
        "Esta sección requiere análisis humano sobre el output crudo. "
        "Sugerencias de qué buscar:"
    )
    lines.append("")
    lines.append("- ¿Hay listado de bandeja con paginación scrapeable?")
    lines.append("- ¿Cuál es el patrón de URL del detalle de una notificación?")
    lines.append("- ¿Cómo se identifican leídas vs no-leídas en el DOM?")
    lines.append("- ¿Hay endpoint AJAX que devuelve JSON o todo es JSF postback?")
    lines.append("- ¿El histórico permite filtros de rango de fechas? ¿Hasta cuándo?")
    lines.append("- ¿Hay export nativo de bandeja a Excel/CSV?")
    lines.append("")

    lines.append("## 8. Riesgos descubiertos durante el crawl")
    lines.append("")
    lines.append("_Completar manualmente con observaciones al revisar los outputs crudos._")
    lines.append("")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines), encoding="utf-8")
    return output_path
