#!/usr/bin/env python3
"""
Build English PDF resume for the test-fullstack developer profile.

Source profile (developers/test-fullstack.json) is bilingual: factual data
(years, stack list, contacts, salary numbers) is language-neutral, but
descriptions of work, projects, education, and notes are in russian.
For an english CV we substitute fully translated english text for those
fields so the resulting PDF contains zero cyrillic — readable for any
english-speaking HR with the default Helvetica font.

Usage:
    py -3 build_resume_en.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (
    HRFlowable,
    KeepTogether,
    ListFlowable,
    ListItem,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
DEV_DIR = PROJECT_ROOT / "config" / "developers"
RESUMES_DIR = PROJECT_ROOT / "data" / "resumes"


# ---------------------------------------------------------------------------
# English narrative — everything russian in the profile is replaced here.
# ---------------------------------------------------------------------------

NAME_EN = "Ivan Sokolov"
ROLE_EN = "Fullstack Developer (React + Node.js, with Java background)"
LOCATION_EN = "Minsk, Belarus  ·  Europe/Minsk (UTC+3)"

SUMMARY_EN = (
    "Fullstack developer with 8 years of experience. Strongest in "
    "React/TypeScript on the frontend and Node.js on the backend. "
    "For the last 3 years leading a small feature team at a B2B SaaS "
    "product company. Comfortable owning architecture, code reviews, "
    "and shipping production features end-to-end."
)

SKILLS_EN = {
    "Core stack": [
        "React", "TypeScript", "JavaScript (ES2022)", "Next.js",
        "Node.js", "Express", "NestJS", "PHP",
        "REST API", "GraphQL (Apollo)",
        "PostgreSQL", "MongoDB",
        "HTML5", "CSS3", "Redux Toolkit", "Zustand",
    ],
    "Strong secondary": [
        "Java 17", "Spring Boot", "JPA/Hibernate",
        "Tailwind CSS", "Styled Components", "SCSS",
        "Jest", "React Testing Library", "Playwright",
        "Docker", "docker-compose",
        "RabbitMQ", "Redis",
        "WebSockets", "Server-Sent Events",
        "Prisma ORM", "Sequelize",
    ],
    "Tools & infra": [
        "Webpack", "Vite", "Turborepo",
        "GitHub Actions", "GitLab CI",
        "Sentry", "Grafana", "Datadog (basics)",
        "Figma (reading handoffs)",
        "Jira", "Linear", "Notion",
        "Swagger/OpenAPI", "Postman",
        "AWS (EC2, S3, RDS, CloudFront — basics)", "Vercel", "Heroku",
    ],
    "Familiar / can adapt quickly": [
        "Vue 3 (read code, small fixes)",
        "Angular (basics)",
        "Python / FastAPI (basics)",
        "Go (read code)",
        "Kubernetes (deployed off-the-shelf charts)",
        "Kafka (integrated as a consumer)",
    ],
}

# (label, level) tuples — order matters (visible in PDF)
SENIOR_TECH_EN = [
    "React", "TypeScript", "Next.js", "Redux / Redux Toolkit",
    "Node.js", "Express", "NestJS", "PostgreSQL",
]

WORK_EN = [
    {
        "period": "2023 — present",
        "role": "Senior Fullstack Developer / Tech Lead",
        "company": "B2B SaaS analytics product company",
        "team": "6 people (3 fullstack, 1 QA, 1 designer, 1 PM)",
        "description": (
            "Lead a feature team. Stack: React 18 + TypeScript + Next.js on "
            "the frontend, Node.js (NestJS) + PostgreSQL + Redis on the "
            "backend. Own architecture for new modules, code reviews, task "
            "breakdown. Built a real-time notifications subsystem on "
            "WebSocket (~20k concurrent connections). Set up CI on GitHub "
            "Actions with unit/e2e test runs and staging deploys."
        ),
        "highlights": [
            "Designed real-time notifications layer "
            "(WebSocket + Redis pub/sub)",
            "Rewrote legacy billing module on NestJS — cut average response "
            "time from 450ms to 120ms",
            "Introduced feature flags (Unleash) for safe rollouts",
        ],
    },
    {
        "period": "2020 — 2023",
        "role": "Middle to Senior Fullstack Developer",
        "company": "Outsourcing company, EU and US clients",
        "team": "4 to 12 across projects",
        "description": (
            "Built SaaS applications for clients. Frontend on React + Redux "
            "Toolkit + TypeScript, backend on Node.js/Express and "
            "occasionally Java/Spring Boot (two projects). Lots of REST, "
            "some GraphQL. Integrations with Stripe, Auth0, SendGrid. "
            "Promoted to Senior in 2022 after owning a reporting module "
            "from scratch."
        ),
        "highlights": [
            "Reporting module with custom charts on recharts",
            "Integrations with Google Ads API and Meta Ads API",
            "Migrated the project to a monorepo (Turborepo)",
        ],
    },
    {
        "period": "2018 — 2020",
        "role": "Middle Frontend Developer (with Node.js)",
        "company": "E-commerce platform for a German retailer (outstaff)",
        "team": "8 people",
        "description": (
            "Frontend on React + Redux, gradually picked up the Node.js BFF "
            "layer (Express). TypeScript was introduced on the project with "
            "my involvement — migrated the codebase incrementally."
        ),
        "highlights": [
            "TypeScript migration",
            "Storybook adoption and visual regression tests",
        ],
    },
    {
        "period": "2017 — 2018",
        "role": "Junior Frontend Developer",
        "company": "Local web studio",
        "team": "3 people",
        "description": (
            "HTML/CSS and JavaScript for online stores and corporate "
            "websites. First exposure to React; before that — vanilla JS "
            "and jQuery."
        ),
        "highlights": [],
    },
]

KEY_PROJECT_HIGHLIGHTS_EXTRA = [
    # Bonus highlight bullets shown under «Selected projects» — these come
    # from key_projects in the profile that aren't already listed under work.
    {
        "name": "Internal CRM for a logistics client",
        "years": "2020 — 2021",
        "stack": "React, TypeScript, Java 11, Spring Boot, PostgreSQL",
        "highlights": [
            "Frontend + about 30% of backend endpoints on Spring",
            "SQL query optimization (EXPLAIN, indexes) — 5–7x faster exports",
        ],
    },
]

EDUCATION_EN = {
    "degree": "Bachelor of Science",
    "field": "Software Engineering",
    "institution": "BSUIR (Belarusian State University of Informatics "
                   "and Radioelectronics)",
    "years": "2010 — 2015",
}

ADDITIONAL_COURSES_EN = [
    ("Stepik — Advanced React Patterns", 2022),
    ("Udemy — Node.js: The Complete Guide", 2021),
    ("EPAM external course — System Design Basics", 2023),
]

LANGUAGES_EN = [
    ("Russian", "native"),
    ("English", "B2 (upper-intermediate) — comfortable in writing and on calls"),
    ("Belarusian", "conversational"),
]

AVAILABILITY_EN = [
    ("Available", "2 weeks after signing"),
    ("Notice period", "2 weeks"),
    ("Working hours", "09:00 – 18:00 MSK, flexible by ±3 hours"),
    ("Overlap", "at least 4 hours overlap with the team; "
                "9:00 – 17:00 UTC works well"),
    ("Engagement formats", "full-time, B2B, contract, part-time (>=30h/week)"),
    ("Compensation", "target $5000/mo; minimum $3500/mo; ~$30/h"),
    ("Notes", "For full-time, comfortable target $5000/mo net. "
              "For B2B/contract +15% to cover taxes and unpaid leave. "
              "Open to USD, EUR, USDT."),
    ("Format", "Remote only"),
]


# ---------------------------------------------------------------------------
# Style sheet — uses default Helvetica (latin-only is fine, no cyrillic)
# ---------------------------------------------------------------------------

def styles() -> dict:
    base = getSampleStyleSheet()
    return {
        "name": ParagraphStyle(
            "name", parent=base["Title"], fontName="Helvetica-Bold",
            fontSize=22, leading=26, spaceAfter=2,
            textColor=colors.HexColor("#1a1a1a"), alignment=TA_LEFT,
        ),
        "role": ParagraphStyle(
            "role", parent=base["Normal"], fontName="Helvetica",
            fontSize=12, leading=15,
            textColor=colors.HexColor("#444444"), spaceAfter=2,
        ),
        "contact": ParagraphStyle(
            "contact", parent=base["Normal"], fontName="Helvetica",
            fontSize=9.5, leading=12,
            textColor=colors.HexColor("#444444"),
        ),
        "h1": ParagraphStyle(
            "h1", parent=base["Heading1"], fontName="Helvetica-Bold",
            fontSize=12, leading=14,
            textColor=colors.HexColor("#1a1a1a"),
            spaceBefore=10, spaceAfter=4,
        ),
        "h2": ParagraphStyle(
            "h2", parent=base["Heading2"], fontName="Helvetica-Bold",
            fontSize=10.5, leading=13,
            textColor=colors.HexColor("#222222"),
            spaceBefore=6, spaceAfter=1,
        ),
        "period": ParagraphStyle(
            "period", parent=base["Normal"], fontName="Helvetica-Oblique",
            fontSize=9, leading=11,
            textColor=colors.HexColor("#666666"), spaceAfter=2,
        ),
        "body": ParagraphStyle(
            "body", parent=base["Normal"], fontName="Helvetica",
            fontSize=10, leading=13.5,
            textColor=colors.HexColor("#222222"), spaceAfter=4,
        ),
        "bullet": ParagraphStyle(
            "bullet", parent=base["Normal"], fontName="Helvetica",
            fontSize=10, leading=13.5,
            textColor=colors.HexColor("#222222"),
            leftIndent=12, bulletIndent=2, spaceAfter=2,
        ),
    }


def section_title(text: str, st: dict) -> list:
    return [
        Paragraph(text.upper(), st["h1"]),
        HRFlowable(width="100%", thickness=0.6,
                   color=colors.HexColor("#bbbbbb"),
                   spaceBefore=0, spaceAfter=4),
    ]


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------

def header(profile: dict, st: dict) -> list:
    links = profile.get("fixed", {}).get("links", {})
    bits = []
    if links.get("email"):
        bits.append(f'<b>Email:</b> {links["email"]}')
    if links.get("telegram"):
        bits.append(f'<b>Telegram:</b> {links["telegram"]}')
    if links.get("linkedin"):
        bits.append(f'<b>LinkedIn:</b> {links["linkedin"]}')
    if links.get("github"):
        bits.append(f'<b>GitHub:</b> {links["github"]}')
    bits.append(f'<b>Location:</b> {LOCATION_EN}')

    return [
        Paragraph(NAME_EN, st["name"]),
        Paragraph(ROLE_EN, st["role"]),
        Paragraph("  &nbsp;|&nbsp;  ".join(bits), st["contact"]),
        Spacer(1, 4),
        HRFlowable(width="100%", thickness=1.0,
                   color=colors.HexColor("#1a1a1a"),
                   spaceBefore=2, spaceAfter=4),
    ]


def summary(_profile: dict, st: dict) -> list:
    return section_title("Summary", st) + [Paragraph(SUMMARY_EN, st["body"])]


def skills(_profile: dict, st: dict) -> list:
    out = section_title("Technical Skills", st)
    for label, items in SKILLS_EN.items():
        out.append(Paragraph(
            f"<b>{label}:</b> " + ", ".join(items), st["body"]))
    out.append(Paragraph(
        "<b>Senior level:</b> " + ", ".join(SENIOR_TECH_EN), st["body"]))
    return out


def experience(_profile: dict, st: dict) -> list:
    out = section_title("Experience", st)
    for w in WORK_EN:
        block: list = []
        title_line = f"<b>{w['role']}</b>"
        if w.get("company"):
            title_line += f"  —  {w['company']}"
        block.append(Paragraph(title_line, st["h2"]))
        meta = w["period"]
        if w.get("team"):
            meta += f"  ·  Team: {w['team']}"
        block.append(Paragraph(meta, st["period"]))
        if w.get("description"):
            block.append(Paragraph(w["description"], st["body"]))
        if w.get("highlights"):
            bullets = ListFlowable(
                [ListItem(Paragraph(h, st["bullet"]),
                          leftIndent=14, value="•") for h in w["highlights"]],
                bulletType="bullet", leftIndent=10,
            )
            block.append(bullets)
        out.append(KeepTogether(block))
    return out


def selected_projects(_profile: dict, st: dict) -> list:
    if not KEY_PROJECT_HIGHLIGHTS_EXTRA:
        return []
    out = section_title("Selected Project", st)
    for p in KEY_PROJECT_HIGHLIGHTS_EXTRA:
        block = [
            Paragraph(f"<b>{p['name']}</b>", st["h2"]),
            Paragraph(f"{p['years']}  ·  Stack: {p['stack']}", st["period"]),
        ]
        if p.get("highlights"):
            bullets = ListFlowable(
                [ListItem(Paragraph(h, st["bullet"]),
                          leftIndent=14, value="•") for h in p["highlights"]],
                bulletType="bullet", leftIndent=10,
            )
            block.append(bullets)
        out.append(KeepTogether(block))
    return out


def education(_profile: dict, st: dict) -> list:
    out = section_title("Education", st)
    edu = EDUCATION_EN
    line = f"<b>{edu['degree']}</b>, {edu['field']}, {edu['institution']}, {edu['years']}"
    out.append(Paragraph(line, st["body"]))

    out.append(Paragraph("<b>Additional courses:</b>", st["body"]))
    items = [ListItem(Paragraph(f"{name} — {year}", st["bullet"]),
                      leftIndent=14, value="•")
             for name, year in ADDITIONAL_COURSES_EN]
    out.append(ListFlowable(items, bulletType="bullet", leftIndent=10))
    return out


def languages_block(_profile: dict, st: dict) -> list:
    out = section_title("Languages", st)
    bits = [f"<b>{n}:</b> {lvl}" for n, lvl in LANGUAGES_EN]
    out.append(Paragraph(" &nbsp;|&nbsp; ".join(bits), st["body"]))
    return out


def availability_block(_profile: dict, st: dict) -> list:
    out = section_title("Availability &amp; Engagement", st)
    rows = AVAILABILITY_EN
    tbl = Table(
        [[Paragraph(f"<b>{k}</b>", st["body"]),
          Paragraph(v, st["body"])] for k, v in rows],
        colWidths=[4.0 * cm, 12.5 * cm],
        hAlign="LEFT",
    )
    tbl.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 1),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
    ]))
    out.append(tbl)
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def load_profile(dev_id: str) -> dict:
    path = DEV_DIR / f"{dev_id}.json"
    if not path.exists():
        sys.exit(f"profile not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def build(dev_id: str) -> Path:
    profile = load_profile(dev_id)
    fixed = profile.get("fixed", {})
    links = fixed.get("links", {})
    out_name = links.get("resume_file_en") or f"{dev_id}-en.pdf"
    out_name = out_name.split("/")[-1]
    out_path = RESUMES_DIR / out_name
    RESUMES_DIR.mkdir(parents=True, exist_ok=True)

    st = styles()
    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=A4,
        leftMargin=1.6 * cm, rightMargin=1.6 * cm,
        topMargin=1.4 * cm, bottomMargin=1.4 * cm,
        title=f"CV — {NAME_EN}",
        author=NAME_EN,
    )
    story: list = []
    story += header(profile, st)
    story += summary(profile, st)
    story += skills(profile, st)
    story += experience(profile, st)
    story += selected_projects(profile, st)
    story += education(profile, st)
    story += languages_block(profile, st)
    story += availability_block(profile, st)

    doc.build(story)
    return out_path


if __name__ == "__main__":
    dev_id = sys.argv[1] if len(sys.argv) > 1 else "test-fullstack"
    p = build(dev_id)
    print(f"wrote {p}  ({p.stat().st_size} bytes)")
