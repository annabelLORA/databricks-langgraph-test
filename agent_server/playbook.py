"""Project Lifecycle Playbook search module."""
import json
import os
from pathlib import Path
from typing import Optional

_DATA_PATH = Path(__file__).parent / "playbook_data.jsonl"

# Load all records once at import time
_RECORDS: list[dict] = []
if _DATA_PATH.exists():
    with open(_DATA_PATH) as f:
        for line in f:
            line = line.strip()
            if line:
                _RECORDS.append(json.loads(line))

# Role alias → canonical playbook function name
_ROLE_ALIASES: dict[str, str] = {
    # P&PC
    "planning controls manager": "Planning & Project Controls",
    "project controls": "Planning & Project Controls",
    "planner": "Planning & Project Controls",
    "pc lead": "Planning & Project Controls",
    "p&pc": "Planning & Project Controls",
    "scheduler": "Planning & Project Controls",
    "planning & project controls": "Planning & Project Controls",
    # Project Leader
    "pm": "Project Leader",
    "project manager": "Project Leader",
    "project lead": "Project Leader",
    "project leader": "Project Leader",
    # PTL
    "ptl": "Project Technical Leader (PTL)",
    "technical lead": "Project Technical Leader (PTL)",
    "design lead": "Project Technical Leader (PTL)",
    "project technical leader": "Project Technical Leader (PTL)",
    # Bid
    "bid manager": "Bid Leader",
    "tender lead": "Bid Leader",
    "bid leader": "Bid Leader",
    # Commercial
    "commercial manager": "Commercial",
    "qs": "Commercial",
    "quantity surveyor": "Commercial",
    "estimator": "Estimator",
    "commercial": "Commercial",
    # Delivery
    "delivery manager": "Delivery Leader",
    "construction manager": "Construction",
    "delivery leader": "Delivery Leader",
    # Package
    "package lead": "Package Manager",
    "package manager": "Package Manager",
    # Commissioning
    "commissioning manager": "Commissioning / Services",
    "services engineer": "Commissioning / Services",
    "commissioning": "Commissioning / Services",
    # Completions
    "completions lead": "Completions",
    "completions": "Completions",
    # Environmental
    "environmental manager": "Environmental",
    "environmental": "Environmental",
    # Sustainability
    "sustainability lead": "Sustainability",
    "sustainability": "Sustainability",
    # Quality
    "quality manager": "Quality",
    "quality": "Quality",
    # HSE
    "hse": "Health & Safety",
    "safety manager": "Health & Safety",
    "health & safety": "Health & Safety",
    # Procurement
    "procurement": "Procurement",
    "buyer": "Procurement",
    # Digital
    "digital": "Digital Delivery",
    "bim lead": "Digital Delivery",
    "digital delivery": "Digital Delivery",
    # Stakeholder
    "stakeholder": "Community & Stakeholder Engagement",
    "community lead": "Community & Stakeholder Engagement",
    # People / HR
    "hr": "People",
    "people lead": "People",
    "people": "People",
    # Risk
    "risk manager": "Risk & Assurance (R&A)",
    "risk": "Risk & Assurance (R&A)",
    # Document control
    "document controller": "Document Controller / Document Management",
    # Site roles
    "site engineer": "Section Engineer",
    "supervisor": "Section Supervision",
    "site manager": "Section Manager",
    "superintendent": "Superintendent",
    # C&M
    "clients & markets": "Clients & Markets",
    "clients and markets": "Clients & Markets",
}

# Phase alias → canonical phase
_PHASE_ALIASES: dict[str, str] = {
    "tender": "Tender",
    "interest": "Tender",
    "bid": "Tender",
    "offer": "Tender",
    "sign": "Tender",
    "pre-contract": "Tender",
    "planning": "Planning",
    "project start-up": "Planning",
    "startup": "Planning",
    "start-up": "Planning",
    "mobilisation": "Planning",
    "mobilization": "Planning",
    "onboarding": "Planning",
    "premobilisation": "Planning",
    "procurement": "Procurement & Supply",
    "procurement & supply": "Procurement & Supply",
    "supply": "Procurement & Supply",
    "design": "Design",
    "masterplan": "Design",
    "concept": "Design",
    "schematic": "Design",
    "preliminary": "Design",
    "detailed design": "Design",
    "final design": "Design",
    "construction": "Construction",
    "commissioning": "Construction",
    "completion": "Completion",
    "handover": "Completion",
    "post-handover": "Completion",
    "dlp": "Completion",
    "demobilisation": "Completion",
    "demobilization": "Completion",
    "multiphase": "Multiphase",
    "ongoing": "Multiphase",
    "multi-phase": "Multiphase",
}

# Sub-phase keywords to match against record subPhase field
_SUBPHASE_ALIASES: dict[str, str] = {
    "interest": "Interest",
    "bid": "Bid",
    "offer": "Offer",
    "sign": "Sign",
    "pre-contract": "Pre-Contract",
    "project start-up": "Project Start-up",
    "start-up": "Project Start-up",
    "startup": "Project Start-up",
    "onboarding": "Onboarding / Pre-mobilisation",
    "premobilisation": "Onboarding / Pre-mobilisation",
    "mobilisation": "Mobilisation",
    "mobilization": "Mobilisation",
    "procurement": "Procurement",
    "supply": "Supply / Off-Site Manufacture",
    "off-site": "Supply / Off-Site Manufacture",
    "masterplan": "Masterplan / Concept",
    "concept": "Masterplan / Concept",
    "schematic": "Schematic / Preliminary",
    "preliminary": "Schematic / Preliminary",
    "detailed design": "Detailed Design",
    "detailed": "Detailed Design",
    "final design": "Final Design",
    "final": "Final Design",
    "construction": "Construction",
    "commissioning": "Commissioning",
    "handover": "Handover",
    "post-handover": "Post-Handover / DLP",
    "dlp": "Post-Handover / DLP",
    "demobilisation": "Demobilisation",
    "demobilization": "Demobilisation",
}


def _resolve_role(raw: str) -> str:
    key = raw.strip().lower()
    return _ROLE_ALIASES.get(key, raw.strip())


def _resolve_phase(raw: str) -> str:
    key = raw.strip().lower()
    return _PHASE_ALIASES.get(key, raw.strip().title())


def _resolve_subphase(raw: str) -> Optional[str]:
    if not raw:
        return None
    key = raw.strip().lower()
    return _SUBPHASE_ALIASES.get(key)


def search_playbook(role: str, phase: str, sub_phase: str = "") -> dict:
    """Search the playbook for processes matching role + phase (+ optional sub-phase).

    Returns a dict with:
      - canonical_role, canonical_phase, canonical_sub_phase
      - p1 (checkpoints), p2 (primary owner), p3 (secondary contributor)
      - total_count
    """
    canonical_role = _resolve_role(role)
    canonical_phase = _resolve_phase(phase)
    canonical_sub = _resolve_subphase(sub_phase) if sub_phase else None

    results = {"p1": [], "p2": [], "p3": []}

    for rec in _RECORDS:
        rec_phase = rec.get("phase", "")
        rec_sub = rec.get("subPhase", "")
        rec_primary = rec.get("primaryResponsible", "")
        rec_secondary = rec.get("secondaryResponsible", [])
        if isinstance(rec_secondary, str):
            rec_secondary = [rec_secondary]
        is_checkpoint = rec.get("functionalCheckpoint", False)

        # Phase matching: include Multiphase records always; otherwise match phase
        phase_match = (
            rec_phase == "Multiphase"
            or rec_phase == canonical_phase
        )
        if not phase_match:
            continue

        # Sub-phase filter: if specified, only include records for that sub-phase
        # (Multiphase / blank subphase records always included)
        if canonical_sub and rec_sub and rec_sub != canonical_sub:
            continue

        # Role matching
        role_as_primary = rec_primary == canonical_role
        role_as_secondary = canonical_role in rec_secondary

        if not role_as_primary and not role_as_secondary:
            continue

        # Prioritise
        if is_checkpoint and (role_as_primary or role_as_secondary):
            bucket = "p1"
        elif role_as_primary:
            bucket = "p2"
        else:
            bucket = "p3"

        results[bucket].append({
            "id": rec.get("id"),
            "title": rec.get("processTitle"),
            "sub_phase": rec_sub,
            "description": rec.get("processDescription", ""),
            "primary": rec_primary,
            "secondary": rec_secondary,
            "is_checkpoint": is_checkpoint,
            "links": rec.get("links", []),
        })

    return {
        "canonical_role": canonical_role,
        "canonical_phase": canonical_phase,
        "canonical_sub_phase": canonical_sub or "",
        "p1": results["p1"],
        "p2": results["p2"],
        "p3": results["p3"],
        "total_count": len(results["p1"]) + len(results["p2"]) + len(results["p3"]),
    }
