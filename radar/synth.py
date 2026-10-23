"""Synthetic supplier updates, written as files into data/input/<SUPPLIER>/.

Batch mode:  python -m radar.synth --days 60 --seed 42
Live mode:   python -m radar.synth --live 10 --interval 15   (drips new files for a demo)
"""
import argparse
import json
import random
import time
from datetime import datetime, timedelta, timezone

from .config import INPUT_DIR, load_suppliers

SITES = {"APEX": "Hsinchu fab", "NORDIC": "Luleå mill", "MERIDIAN": "Port Klang hub",
         "VERDANT": "Ludwigshafen plant", "SUNRISE": "Gazipur factory"}
LOOKALIKE = {"APEX": "Apex Hiking Gear", "NORDIC": "Nordic Steelworks Hockey Club",
             "MERIDIAN": "Meridian Hotel Group", "VERDANT": "Verdant Landscaping Ltd",
             "SUNRISE": "Sunrise Bakery"}

NOISE = [
    ("news", "{name} wins regional sustainability award",
     "{name} was recognised at the annual industry awards for its energy-efficiency programme at {site}."),
    ("news", "{name} to exhibit at upcoming trade fair",
     "{name} will showcase its latest product range at the international trade fair next month. Booth details on its website."),
    ("social", "We're hiring at {name}!",
     "Join our growing team at {site}. Open roles in engineering, quality and finance. #careers #hiring"),
    ("social", "Team volunteering day",
     "Proud of our {site} colleagues who spent Saturday planting trees with the local community. 🌱"),
    ("email", "{name} quarterly customer newsletter",
     "Dear valued customer, in this issue: product updates, a new customer portal login page and holiday greetings from our team."),
    ("email", "Updated contact list for your account team",
     "Please note your account manager remains unchanged. Attached is the refreshed escalation contact list for {name}."),
    ("erp", "PO-{po} acknowledged, on schedule",
     "Purchase order PO-{po} confirmed by {name}. Planned ship date unchanged. No exceptions recorded."),
    ("erp", "ASN received for PO-{po}",
     "Advance shipping notice received for PO-{po}. Quantities match order. Expected arrival on time."),
    ("rating", "Rating agency affirms {name} at stable outlook",
     "The agency affirmed its rating for {name} with a stable outlook, citing steady margins and adequate liquidity."),
    ("news", "{name} CEO interview on industry trends",
     "In an interview, the {name} chief executive discussed automation and long-term demand, without new guidance."),
    ("news", "{lookalike} opens new store downtown",
     "{lookalike}, a local business unrelated to industrial suppliers, celebrated its new location this weekend."),
    ("news", "{name} launches redesigned website",
     "{name} unveiled a new website with improved product search and a refreshed brand identity."),
    ("email", "Planned maintenance window, no impact expected",
     "Routine maintenance at {site} is scheduled for Sunday night. Production plans and deliveries are not affected."),
    ("social", "{name} celebrates 25 years",
     "Thank you to our customers and employees for 25 years of partnership! 🎉"),
]

MINOR_SIGNALS = [
    ("erp", "PO-{po} delayed 3 days",
     "Shipment for PO-{po} from {name} rescheduled by 3 days due to a carrier capacity issue. Root cause: missed truck slot."),
    ("email", "Price adjustment notice from {name}",
     "{name} informs of a 4% price increase on selected items from next quarter, citing input cost inflation."),
    ("erp", "Quality deviation on lot L-{po}",
     "Incoming inspection found 1.8% defects on lot L-{po} from {name}, above the 1% AQL. Lot quarantined for sorting."),
]

# Storylines: (day offset from start, source, title, body)
STORIES = {
    "APEX": [
        (38, "news", "Fire at {site} halts production on line 3",
         "A fire broke out overnight at {name}'s {site}. Line 3, which produces power-management chips, is halted. "
         "The company estimates at least three weeks before restart."),
        (40, "erp", "PO-{po} delayed 14 days",
         "{name} pushed the ship date of PO-{po} by 14 days. Reason code: production stoppage."),
        (42, "email", "Force majeure notice and allocation",
         "{name} declares force majeure on deliveries from {site} and moves customers to 60% allocation until further notice."),
        (46, "rating", "Rating agency places {name} on negative credit watch",
         "The agency placed {name} on CreditWatch negative, citing fire-related losses, insurance uncertainty and weaker liquidity."),
        (49, "social", "Employees report layoffs at {name}",
         "Several posts from {name} staff report temporary layoffs of contract workers at {site} after the fire."),
    ],
    "MERIDIAN": [
        (20, "news", "Dockworkers at {site} vote on strike action",
         "Unions representing dockworkers at the {site} used by {name} announced a strike ballot over pay."),
        (23, "news", "Strike begins at {site}, terminal operations slowed",
         "Dockworkers walked out at the {site}. {name} warns customers of berth congestion and container backlogs."),
        (24, "erp", "PO-{po} shipment delayed 8 days",
         "{name} reports vessel departure for PO-{po} slipped 8 days due to terminal congestion."),
        (27, "email", "Vessel diversions and emergency surcharge",
         "{name} is diverting two vessels and applying an emergency congestion surcharge of USD 450 per container."),
        (31, "erp", "Three open POs at risk of missing line-down date",
         "Logistics control tower flags three open POs routed via {name} at risk of arriving after the production need date."),
    ],
    "SUNRISE": [
        (50, "rating", "{name} fails third-party social compliance audit",
         "An independent audit of {name}'s {site} found excessive overtime, blocked fire exits and missing wage records."),
        (53, "news", "Wage protests close {site}",
         "Workers protesting unpaid wages forced {name} to suspend operations at {site} for three days."),
        (56, "news", "Major apparel brand suspends orders from {name}",
         "A global apparel brand said it has suspended new orders from {name} pending remediation of audit findings."),
    ],
    "VERDANT": [
        (33, "email", "Regulator notes labelling discrepancy, corrective action filed",
         "The regional authority noted a minor safety-data-sheet labelling discrepancy at {name}. A corrective action was filed and closed within a week."),
    ],
    "NORDIC": [
        (15, "email", "Energy surcharge introduced by {name}",
         "{name} introduces a temporary 3% energy surcharge due to higher electricity prices in northern Sweden."),
        (19, "erp", "PO-{po} delayed 2 days",
         "PO-{po} from {name} delayed 2 days because of a rail freight scheduling change."),
    ],
}


def _fill(t: str, sup: dict, rnd: random.Random) -> str:
    return t.format(name=sup["name"], site=SITES[sup["id"]], lookalike=LOOKALIKE[sup["id"]],
                    po=rnd.randint(1000, 9999))


def _write(sup, when, src, title, body, rnd, seq):
    d = INPUT_DIR / sup["id"]
    d.mkdir(parents=True, exist_ok=True)
    fname = f"{when:%Y%m%d_%H%M}_{src}_{seq:04d}.json"
    rec = {"supplier_id": sup["id"], "source_type": src, "published_at": when.isoformat(),
           "title": _fill(title, sup, rnd), "body": _fill(body, sup, rnd)}
    (d / fname).write_text(json.dumps(rec, indent=2, ensure_ascii=False))
    return d / fname


def batch(days: int, seed: int) -> int:
    rnd = random.Random(seed)
    start = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) - timedelta(days=days)
    n = 0
    for sup in load_suppliers():
        for _ in range(rnd.randint(26, 34)):                      # noise
            when = start + timedelta(days=rnd.uniform(0, days), hours=rnd.randint(0, 23))
            _write(sup, when, *rnd.choice(NOISE), rnd, n); n += 1
        for _ in range(rnd.randint(0, 2)):                        # sparse isolated minor signals
            when = start + timedelta(days=rnd.uniform(0, days * 0.4))
            _write(sup, when, *rnd.choice(MINOR_SIGNALS), rnd, n); n += 1
        for day, src, title, body in STORIES.get(sup["id"], []):  # storylines
            if day <= days:
                _write(sup, start + timedelta(days=day, hours=rnd.randint(6, 18)), src, title, body, rnd, n); n += 1

    # A few operational edge cases: a duplicate, a malformed file, a plain-text email
    sup = load_suppliers()[0]
    first = sorted((INPUT_DIR / sup["id"]).glob("*.json"))[0]
    (INPUT_DIR / sup["id"] / f"dup_{first.name}").write_text(first.read_text()); n += 1
    (INPUT_DIR / "VERDANT" / "corrupt_export_0001.json").write_text('{"title": "Truncated', encoding="utf-8"); n += 1
    (INPUT_DIR / "NORDIC" / "email_forward.txt").write_text(
        "FW: Holiday schedule\nOur office will be closed on public holidays. Deliveries continue as planned.\n"); n += 1
    print(f"Wrote {n} synthetic update files to {INPUT_DIR}")
    return n


def live(count: int, interval: float, seed: int | None) -> None:
    rnd = random.Random(seed)
    sups = load_suppliers()
    for i in range(count):
        sup = rnd.choice(sups)
        tmpl = rnd.choice(MINOR_SIGNALS) if rnd.random() < 0.3 else rnd.choice(NOISE)
        p = _write(sup, datetime.now(timezone.utc), *tmpl, rnd, int(time.time()) % 10000 + i)
        print(f"[{i + 1}/{count}] dropped {p.relative_to(INPUT_DIR)}")
        if i < count - 1:
            time.sleep(interval)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--live", type=int, help="drip N new files instead of a batch")
    ap.add_argument("--interval", type=float, default=10, help="seconds between live files")
    a = ap.parse_args()
    live(a.live, a.interval, None) if a.live else batch(a.days, a.seed)
