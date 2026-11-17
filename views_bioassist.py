"""BioAssist: manual-grounded retrieval plus an optional LLM provider.

Answer modes are explicit so a clinician-facing reader can tell the difference:

* ``manual_cited``  - generated text that cites extracts from uploaded manuals
* ``manual_only``   - no provider configured/unavailable; verbatim manual excerpts
* ``general_ai``    - generated text with no manual support in this hospital
* ``provider_error``- the provider failed; the reason is shown

BioAssist never certifies equipment, authorises repairs, or overrides warnings.
"""

import json
import re
import urllib.error
import urllib.request

from flask import Blueprint, current_app, jsonify, render_template, request

import core
from core import audit, current_user, engineer_or_admin, query, roles_required
from db import now_iso

bp = Blueprint("bioassist", __name__)

DISCLAIMER = (
    "BioAssist supports qualified biomedical engineers. It does not replace "
    "engineering judgement, manufacturer instructions or safety procedures. It "
    "cannot certify equipment as safe or authorise a repair."
)

STOP_WORDS = {
    "the", "a", "an", "and", "or", "of", "for", "to", "in", "on", "is", "are",
    "how", "what", "why", "with", "this", "that", "does", "do", "it", "be",
    "can", "should", "my", "we", "i", "at", "from", "as", "by", "not",
}


@bp.get("/bioassist")
@engineer_or_admin
def bioassist():
    user = current_user()
    equipment = query(
        """SELECT id, name, asset_number FROM equipment
            WHERE hospital_id = ? AND status != 'retired' ORDER BY name""",
        (user["hospital_id"],),
    )
    documents = query(
        """SELECT d.id, d.title, d.page_count, e.name AS equipment_name
             FROM service_documents d LEFT JOIN equipment e ON e.id = d.equipment_id
            WHERE d.hospital_id = ? ORDER BY d.uploaded_at DESC""",
        (user["hospital_id"],),
    )
    configured = bool(current_app.config.get("BIOASSIST_API_KEY"))
    return render_template(
        "bioassist.html", equipment=equipment, documents=documents,
        configured=configured, disclaimer=DISCLAIMER,
        model=current_app.config["BIOASSIST_MODEL"],
    )


@bp.post("/api/bioassist/ask")
@engineer_or_admin
def ask():
    user = current_user()
    payload = request.get_json(silent=True) or request.form
    question = (payload.get("question") or "").strip()
    equipment_id = payload.get("equipment_id") or None
    document_id = payload.get("document_id") or None
    try:
        equipment_id = int(equipment_id) if equipment_id else None
    except (TypeError, ValueError):
        equipment_id = None
    try:
        document_id = int(document_id) if document_id else None
    except (TypeError, ValueError):
        document_id = None

    if len(question) < 5:
        return jsonify({"error": "Ask a longer question (at least 5 characters)."}), 400

    equipment = None
    if equipment_id:
        equipment = query(
            """SELECT id, name, manufacturer, model_number, serial_number, status,
                      location, next_maintenance_date, clinical_use_blocked
                 FROM equipment WHERE id = ? AND hospital_id = ?""",
            (equipment_id, user["hospital_id"]),
            one=True,
        )

    excerpts = _search_manuals(user["hospital_id"], question, document_id, equipment_id)
    context = _format_excerpts(excerpts)
    provider = _call_provider(question, context, equipment)

    if provider["ok"] and provider["text"]:
        mode = "manual_cited" if excerpts else "general_ai"
        answer = provider["text"]
    elif excerpts:
        mode = "manual_only"
        answer = ("No AI provider is configured or reachable, so BioAssist is showing "
                  "matching manual excerpts without generated commentary. Read the "
                  "cited pages before acting.")
    else:
        mode = "provider_error"
        answer = provider["error"] or (
            "BioAssist could not produce an answer. No AI provider is configured and no "
            "matching manual text was found in this hospital."
        )

    audit("bioassist.query", "equipment", equipment_id,
          f"mode={mode}; question={question[:180]}")
    return jsonify({
        "mode": mode,
        "answer": answer,
        "excerpts": [
            {"document": e["title"], "page": e["page_no"], "text": e["text"][:900]}
            for e in excerpts
        ],
        "disclaimer": DISCLAIMER,
        "provider": current_app.config["BIOASSIST_MODEL"] if provider["ok"] else None,
    })


def _keywords(question):
    words = re.findall(r"[a-zA-Z0-9\-]{3,}", question.lower())
    return [w for w in words if w not in STOP_WORDS][:8]


def _search_manuals(hospital_id, question, document_id, equipment_id):
    """Simple scored search over extracted manual text, tenant-scoped."""
    keys = _keywords(question)
    if not keys:
        return []
    sql = """SELECT d.title, p.page_no, p.text, d.equipment_id
               FROM document_pages p
               JOIN service_documents d ON d.id = p.document_id
              WHERE d.hospital_id = ?"""
    params = [hospital_id]
    if document_id:
        sql += " AND d.id = ?"
        params.append(document_id)
    # always tenant-scoped; prefer docs linked to the selected equipment
    rows = query(sql, params)
    scored = []
    for row in rows:
        text = (row["text"] or "").lower()
        score = sum(text.count(k) for k in keys)
        if score:
            if equipment_id and row["equipment_id"] == equipment_id:
                score += 3
            scored.append((score, row))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [row for _, row in scored[:4]]


def _format_excerpts(excerpts):
    if not excerpts:
        return ""
    blocks = []
    for ex in excerpts:
        blocks.append(f"[{ex['title']}, page {ex['page_no']}]\n{ex['text'][:1400]}")
    return "\n\n".join(blocks)


SYSTEM_PROMPT = (
    "You are BioAssist, a decision-support tool inside a hospital biomedical "
    "engineering system in Uganda. Answer only as an assistant to a qualified "
    "biomedical engineer. Rules: never state that equipment is safe for clinical "
    "use, never authorise a repair, never override or soften a safety warning. "
    "If you use the supplied manual excerpts, cite them as [document, page N]. "
    "If the excerpts do not cover the question, say so plainly and mark the answer "
    "as a general explanation rather than manufacturer documentation. Keep the "
    "answer concise and practical, and end with a one-line safety reminder that a "
    "qualified engineer must verify the equipment before clinical use."
)


def _call_provider(question, context, equipment):
    api_key = current_app.config.get("BIOASSIST_API_KEY")
    if not api_key:
        return {"ok": False, "text": None,
                "error": "No AI provider is configured for this server."}
    url = current_app.config["BIOASSIST_API_URL"]
    model = current_app.config["BIOASSIST_MODEL"]
    user_content = f"Engineer question: {question}"
    if equipment:
        user_content += (
            f"\n\nSelected equipment (single record): {equipment['name']}, "
            f"{equipment['manufacturer']} {equipment['model_number']}, "
            f"serial {equipment['serial_number']}, status {equipment['status']}, "
            f"location {equipment['location']}, next maintenance "
            f"{equipment['next_maintenance_date']}"
        )
    if context:
        user_content += f"\n\nManual excerpts from this hospital:\n{context}"
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.2,
        "max_tokens": 700,
    }).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return {"ok": False, "text": None,
                "error": f"The AI provider returned an error (HTTP {exc.code}). "
                         f"Manual-based results are still available."}
    except (urllib.error.URLError, TimeoutError, OSError):
        return {"ok": False, "text": None,
                "error": "The AI provider could not be reached. Manual-based results "
                         "are still available."}
    except ValueError:
        return {"ok": False, "text": None,
                "error": "The AI provider returned an unreadable response."}
    try:
        text = data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError):
        return {"ok": False, "text": None,
                "error": "The AI provider response did not contain an answer."}
    return {"ok": True, "text": text, "error": None}
