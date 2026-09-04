"""The party and preset data a USPTO filing needs, and the rules that gate it.

One JSON file under ~/.tmux-dashboard/patents/. Everything a submission needs that
is NOT in the draft itself lives here: inventors with their residence and mailing
address, applicants, correspondence profiles, practitioners, and named presets that
bundle them. The panel edits it; the filing agent reads it.

The one piece of real logic in here is `representation_gate`. Since 20 July 2026
(37 CFR 1.31(a), 91 FR 13519) a filing must go through a registered practitioner if
the applicant is a juristic entity, or if any party identified as the applicant is
domiciled outside the United States. Under 37 CFR 1.42(a) the applicant is "the
inventor or ALL of the joint inventors" unless a 1.46 applicant is named, so adding
one Israel-resident co-inventor silently ends the pro se route for the whole
application. That is the trap this module exists to catch before a packet is built,
not after the ADS is signed.
"""
from __future__ import annotations

import copy
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path

DATA_DIR = Path(os.environ.get("PATENT_DATA_DIR",
                               Path.home() / ".tmux-dashboard" / "patents"))
STORE_PATH = DATA_DIR / "store.json"
FORMS_DIR = DATA_DIR / "forms"
PACKETS_DIR = DATA_DIR / "packets"
SIGNATURES_DIR = DATA_DIR / "signatures"
SAMPLES_DIR = DATA_DIR / "samples"
OBSERVATIONS_DIR = DATA_DIR / "observations"
# Its own tree: SAMPLES_DIR is walked flat by the filing demo, so a subdirectory
# in there is copied as if it were a file and the filing demo 500s.
OBS_SAMPLES_DIR = DATA_DIR / "samples_observations"
AUTH_DIR = DATA_DIR / "auth"

_LOCK = threading.RLock()

# US fee schedule, verified against uspto.gov 2026-08-30. Small-entity utility
# filing uses fee code 4011 ($70), the ELECTRONIC rate; the $140 row (2011) is the
# paper rate and never applies to a Patent Center submission.
FEES = {
    "utility_basic_electronic": {"code": "4011", "large": None, "small": 70, "micro": None},
    "utility_basic_paper":      {"code": "1011/2011/3011", "large": 350, "small": 140, "micro": 70},
    "utility_search":           {"code": "1111/2111/3111", "large": 770, "small": 308, "micro": 154},
    "utility_examination":      {"code": "1311/2311/3311", "large": 880, "small": 352, "micro": 176},
    "excess_independent":       {"code": "1201/2201/3201", "large": 600, "small": 240, "micro": 120},
    "excess_claims":            {"code": "1202/2202/3202", "large": 200, "small": 80, "micro": 40},
    "multiple_dependent":       {"code": "1203/2203/3203", "large": 925, "small": 370, "micro": 185},
    "app_size_per_50":          {"code": "1081/2081/3081", "large": 440, "small": 176, "micro": 88},
    "non_docx_surcharge":       {"code": "1054/2054/3054", "large": 430, "small": 172, "micro": 86},
    "late_oath_surcharge":      {"code": "1051/2051/3051", "large": 170, "small": 68, "micro": 34},
    "processing_1_17_i":        {"code": "1830/2830/3830", "large": 150, "small": 60, "micro": 30},
    "prioritized_exam":         {"code": "1817/2817/3817", "large": 4515, "small": 1806, "micro": 903},
}
FEE_SCHEDULE_VERIFIED = "2026-08-30"

# 37 CFR 1.29: micro entity is unavailable to anyone named on more than four
# earlier US nonprovisionals. Nimrod Rotem is named on 30+, so the panel never
# offers micro for him; it stays selectable for a genuinely new inventor.
ENTITY_STATUSES = ["small", "large", "micro"]


def _uid(prefix: str) -> str:
    return "%s_%s" % (prefix, uuid.uuid4().hex[:10])


def _now() -> float:
    return time.time()


# --------------------------------------------------------------------------
# seed
# --------------------------------------------------------------------------
def _seed() -> dict:
    """Real parties, read out of the USPTO record for this portfolio on 2026-08-30.

    Addresses come from the inventorBag of the applications already on file, so
    what goes on a new ADS matches what the Office already holds for the same
    person. Where the Office only has a city (it does not publish inventor street
    addresses) the street field is left empty and flagged, rather than invented.
    """
    return {
        "version": 1,
        "updated": _now(),
        "inventors": [
            {
                "id": "inv_nimrod", "given": "Nimrod", "middle": "", "family": "Rotem",
                "suffix": "", "prefix": "",
                "residence": {"city": "Las Vegas", "state": "NV", "country": "US"},
                "mailing": {"line1": "6000 S Eastern Ave", "line2": "Unit 9E",
                            "city": "Las Vegas", "state": "NV", "postal": "89119",
                            "country": "US"},
                "email": "nimo@rotem.ai", "phone": "628-236-9320",
                "signature_file": "", "age_65_plus": False, "date_of_birth": "1986-10-19",
                "notes": ("US domicile. Earlier applications filed by the firm record him at "
                          "Sheung Wan, Hong Kong; that address cannot be used on a pro se "
                          "filing because of 37 CFR 1.31(a)(2). Used on 19/791,470."),
                "uspto_alt_addresses": ["Sheung Wan, HONG KONG", "San Francisco, CA, US"],
            },
            {
                "id": "inv_efraim", "given": "Efraim", "middle": "", "family": "Rotem",
                "suffix": "", "prefix": "",
                "residence": {"city": "Santa Clara", "state": "CA", "country": "US"},
                "mailing": {"line1": "291 Woodhams Dr.", "line2": "", "city": "Santa Clara",
                            "state": "CA", "postal": "95051", "country": "US"},
                "email": "", "phone": "",
                "signature_file": "", "age_65_plus": True, "date_of_birth": "",
                "notes": ("65 or older: qualifies the application for a free Petition to Make "
                          "Special under 37 CFR 1.102(c)(1) whenever he is a named inventor. "
                          "Mailing address read out of the ADS filed in US 19/428,078 on "
                          "2025-12-19, so it matches what the Office already holds."),
                "uspto_alt_addresses": [],
            },
            {
                "id": "inv_ariel", "given": "Ariel", "middle": "", "family": "Rotem",
                "suffix": "", "prefix": "",
                "residence": {"city": "Hoboken", "state": "NJ", "country": "US"},
                "mailing": {"line1": "333 River St.", "line2": "", "city": "Hoboken",
                            "state": "NJ", "postal": "07030", "country": "US"},
                "email": "", "phone": "",
                "signature_file": "", "age_65_plus": False, "date_of_birth": "",
                "notes": ("Mailing address read out of the ADS filed in US 19/428,078 on "
                          "2025-12-19."),
                "uspto_alt_addresses": [],
            },
            {
                "id": "inv_oleg", "given": "Oleg", "middle": "", "family": "Joukov",
                "suffix": "", "prefix": "",
                "residence": {"city": "Shaar Efraim", "state": "", "country": "IL"},
                "mailing": {"line1": "Almog 118", "line2": "", "city": "Shaar Efraim",
                            "state": "", "postal": "4283500", "country": "IL"},
                "email": "", "phone": "",
                "signature_file": "", "age_65_plus": False, "date_of_birth": "",
                "notes": ("NON-US DOMICILE. Naming him as a joint inventor makes him part of "
                          "the applicant under 37 CFR 1.42(a), which forces a registered "
                          "practitioner on the whole application under 1.31(a)(2). The USPTO "
                          "record also carries two spellings, 'OIeg Joukov' with a capital i "
                          "and 'Oleg Zhukov'; use Oleg Joukov."),
                "uspto_alt_addresses": [],
            },
            {
                "id": "inv_eduard", "given": "Eduard", "middle": "", "family": "Tsfasman",
                "suffix": "", "prefix": "",
                "residence": {"city": "Shaar Efraim", "state": "", "country": "IL"},
                "mailing": {"line1": "Almog 118", "line2": "", "city": "Shaar Efraim",
                            "state": "", "postal": "4283500", "country": "IL"},
                "email": "", "phone": "",
                "signature_file": "", "age_65_plus": False, "date_of_birth": "",
                "notes": ("NON-US DOMICILE, same practitioner consequence as Oleg Joukov. "
                          "Address from the ADS filed in US 29/923,694."),
                "uspto_alt_addresses": [],
            },
        ],
        "applicants": [
            {
                "id": "app_inventors", "kind": "inventors", "name": "The named inventors",
                "address": {}, "entity_status": "small",
                "notes": ("The default. Leaves ADS section 7 blank, keeps the inventors as the "
                          "applicant, and is the only configuration in which Nimo can sign and "
                          "file without a practitioner."),
            },
            {
                "id": "app_grabo_llc", "kind": "juristic", "name": "GRABO LLC",
                "address": {"line1": "6000 South Eastern Avenue", "line2": "Suite 9E",
                            "city": "Las Vegas", "state": "NV", "postal": "89119",
                            "country": "US"},
                "entity_status": "small",
                "notes": "Nevada LLC, EIN 85-3173437, also trades as Nemo Power Tools LLC.",
            },
            {
                "id": "app_grabo_ltd", "kind": "juristic", "name": "GRABO Limited",
                "address": {"line1": "31/F unit 31-111E, Tower 5, The Gateway, Harbour City",
                            "line2": "15 Canton Road, Tsim Sha Tsui, Kowloon",
                            "city": "Hong Kong", "state": "", "postal": "",
                            "country": "HK"},
                "entity_status": "small",
                "notes": "HK parent, CR 2091524, BR/IRD 63290002. Applicant on most of the portfolio.",
            },
            {
                "id": "app_nebula", "kind": "juristic", "name": "Nebula Innovations LLC",
                "address": {"line1": "6000 S Eastern Ave", "line2": "Unit 9E",
                            "city": "Las Vegas", "state": "NV", "postal": "89119",
                            "country": "US"},
                "entity_status": "small",
                "notes": "Nevada LLC, EIN 81-1446236. Applicant on 19/309,300.",
            },
        ],
        "correspondence": [
            {
                "id": "corr_nimo", "label": "Nimo direct (pro se)",
                "customer_number": "", "name": "Nimrod Rotem",
                "line1": "6000 S Eastern Ave", "line2": "Unit 9E",
                "city": "Las Vegas", "state": "NV", "postal": "89119", "country": "US",
                "email1": "nimo@rotem.ai", "email2": "nimrod.rotem@gmail.com",
                "phone": "628-236-9320",
                "notes": "Used on 19/791,470. Two emails so a notice cannot be missed.",
            },
            {
                "id": "corr_intellent", "label": "Intellent Patents LLC (customer 117228)",
                "customer_number": "117228", "name": "Intellent Patents LLC",
                "line1": "1187 Valley Quail Cir", "line2": "",
                "city": "San Jose", "state": "CA", "postal": "95120-4132", "country": "US",
                "email1": "", "email2": "", "phone": "",
                "notes": ("The firm of record on the recent portfolio. Putting this customer "
                          "number on a new filing sends every notice to them, not to you."),
            },
        ],
        "practitioners": [
            {
                "id": "prac_alhafidh", "name": "Ahmed Alhafidh", "registration": "71158",
                "category": "AGENT", "firm": "Intellent Patents LLC",
                "customer_number": "117228", "active": True,
                "notes": "Practitioner of record on 19/404,012, 19/428,078, 19/428,086, 19/309,300.",
            },
            {
                "id": "prac_rowe", "name": "Jonathan P. Rowe", "registration": "65335",
                "category": "ATTNY", "firm": "Ballard Spahr LLP",
                "customer_number": "111739", "active": True,
                "notes": "On the older cases, customer number 111739.",
            },
        ],
        "payment": [
            {
                "id": "pay_ramp_uspto", "label": "Ramp Visa 0449 (USPTO filing fees)",
                "advisor_key": "ramp-uspto-filing-fees", "last_four": "0449",
                "cap": "$2,500 / month",
                "notes": ("Number and CVV live in the advisor, never here. The agent fetches "
                          "them with get_payment_method at payment time. Proven on 2026-08-30: "
                          "paid the $730 on US 19/791,470."),
            },
            {
                "id": "pay_ramp_ip", "label": "Ramp Visa 6021 (IP filing, patents and trademarks)",
                "advisor_key": "", "last_four": "6021", "cap": "$10,000 / month",
                "notes": ("Exists in Ramp (card id e85711ed-4295-45f5-b1b3-be13b0ea5603) but its "
                          "number has never been read out, so it is not usable at a checkout yet. "
                          "ramp_card_details can reveal it; then save_payment_method puts it in "
                          "the advisor and this row gets an advisor key."),
            },
        ],
        "accounts": [
            {
                "id": "acct_uspto", "label": "USPTO.gov (Patent Center, fees.uspto.gov)",
                "username": "nimrod.rotem@gmail.com", "advisor_secret": "uspto-account",
                "customer_numbers": "117228 (Intellent Patents LLC), 111739 (Ballard Spahr)",
                "notes": ("Password lives in the advisor, never here: get_secret uspto-account. "
                          "MFA is skipped by the Okta device-trust cookies in "
                          "~/.tmux-dashboard/patents/auth/uspto_device.json, valid to Sep 2027, "
                          "re-saved after every login so the year-long token keeps rolling."),
            },
            {
                "id": "acct_odp", "label": "USPTO Open Data Portal API",
                "username": "nimrod.rotem@gmail.com", "advisor_secret": "uspto-odp",
                "customer_numbers": "",
                "notes": ("Read-only. Used to pull the file wrapper for any published "
                          "application, including the ADS an address can be read out of."),
            },
        ],
        "defaults": {
            "entity_status": "small",
            "docket_prefix": "ROTEM",
            "publication": "normal",          # normal | early | nonpublication
            "correspondence_id": "corr_nimo",
            "applicant_id": "app_inventors",
            "payment_id": "pay_ramp_uspto",
            "authorize_pdx": True,            # leave both 1.14(c) opt-out boxes unchecked
            "spec_format": "docx",            # avoids the 1.16(u) surcharge
        },
        "presets": [
            {
                "id": "pre_nimo_prose", "label": "Nimo solo, pro se, small entity",
                "inventor_ids": ["inv_nimrod"], "applicant_id": "app_inventors",
                "correspondence_id": "corr_nimo", "entity_status": "small",
                "publication": "normal", "practitioner_id": "",
                "notes": "Exactly how 19/791,470 went in on 2026-08-30.",
            },
            {
                "id": "pre_nimo_efraim", "label": "Nimo + Efraim, pro se, age petition",
                "inventor_ids": ["inv_nimrod", "inv_efraim"], "applicant_id": "app_inventors",
                "correspondence_id": "corr_nimo", "entity_status": "small",
                "publication": "normal", "practitioner_id": "",
                "notes": ("Both US-domiciled, so pro se still works, and Efraim being 65+ "
                          "makes the free 1.102(c)(1) age petition available."),
            },
            {
                "id": "pre_grabo_firm", "label": "GRABO Limited applicant (firm must file)",
                "inventor_ids": ["inv_nimrod", "inv_oleg"], "applicant_id": "app_grabo_ltd",
                "correspondence_id": "corr_intellent", "entity_status": "small",
                "publication": "normal", "practitioner_id": "prac_alhafidh",
                "notes": "Juristic applicant plus a non-US inventor: a practitioner is mandatory.",
            },
        ],
        "filings": [],
    }


# --------------------------------------------------------------------------
# load / save
# --------------------------------------------------------------------------
def load() -> dict:
    with _LOCK:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        FORMS_DIR.mkdir(parents=True, exist_ok=True)
        PACKETS_DIR.mkdir(parents=True, exist_ok=True)
        SIGNATURES_DIR.mkdir(parents=True, exist_ok=True)
        OBSERVATIONS_DIR.mkdir(parents=True, exist_ok=True)
        if not STORE_PATH.exists():
            data = _seed()
            _write(data)
            return data
        try:
            with STORE_PATH.open(encoding="utf8") as fh:
                data = json.load(fh)
            # Forward-migrate: a store written before a section existed must gain it,
            # or the panel renders an empty tab and the reason is invisible.
            seed, changed = _seed(), False
            for key, value in seed.items():
                if key not in data:
                    data[key] = value
                    changed = True
            for key, value in (seed.get("defaults") or {}).items():
                if key not in (data.get("defaults") or {}):
                    data.setdefault("defaults", {})[key] = value
                    changed = True
            for row in data.get("inventors") or []:
                if "signature_file" not in row:
                    row["signature_file"] = ""
                    changed = True
            if changed:
                _write(data)
            return data
        except Exception:
            # Never lose the file to a parse error: keep it and start clean beside it.
            backup = STORE_PATH.with_suffix(".corrupt-%d.json" % int(_now()))
            try:
                STORE_PATH.replace(backup)
            except Exception:
                pass
            data = _seed()
            _write(data)
            return data


def _write(data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    data["updated"] = _now()
    tmp = STORE_PATH.with_suffix(".tmp")
    with tmp.open("w", encoding="utf8") as fh:
        json.dump(data, fh, indent=1, ensure_ascii=False)
    tmp.replace(STORE_PATH)


def save(data: dict) -> dict:
    with _LOCK:
        _write(data)
        return data


COLLECTIONS = ("inventors", "applicants", "correspondence", "practitioners",
               "payment", "presets", "accounts")
_PREFIX = {"inventors": "inv", "applicants": "app", "correspondence": "corr",
           "practitioners": "prac", "payment": "pay", "presets": "pre",
           "accounts": "acct"}


def upsert(collection: str, row: dict) -> dict:
    """Add or update one row. Returns the stored row."""
    if collection not in COLLECTIONS:
        raise KeyError(collection)
    with _LOCK:
        data = load()
        rows = data.setdefault(collection, [])
        rid = (row.get("id") or "").strip()
        if not rid:
            rid = _uid(_PREFIX[collection])
            row = dict(row, id=rid)
        for i, existing in enumerate(rows):
            if existing.get("id") == rid:
                merged = dict(existing)
                merged.update(row)
                rows[i] = merged
                save(data)
                return merged
        rows.append(row)
        save(data)
        return row


def delete(collection: str, row_id: str) -> bool:
    if collection not in COLLECTIONS:
        raise KeyError(collection)
    with _LOCK:
        data = load()
        rows = data.get(collection) or []
        keep = [r for r in rows if r.get("id") != row_id]
        if len(keep) == len(rows):
            return False
        data[collection] = keep
        # Do not leave presets pointing at a row that no longer exists.
        for p in data.get("presets") or []:
            if collection == "inventors":
                p["inventor_ids"] = [i for i in (p.get("inventor_ids") or []) if i != row_id]
            for field, coll in (("applicant_id", "applicants"),
                                ("correspondence_id", "correspondence"),
                                ("practitioner_id", "practitioners")):
                if coll == collection and p.get(field) == row_id:
                    p[field] = ""
        save(data)
        return True


def find(data: dict, collection: str, row_id: str) -> dict:
    for r in data.get(collection) or []:
        if r.get("id") == row_id:
            return r
    return {}


# --------------------------------------------------------------------------
# the rules
# --------------------------------------------------------------------------
def full_name(inv: dict) -> str:
    parts = [inv.get("given", ""), inv.get("middle", ""), inv.get("family", "")]
    name = " ".join(p for p in parts if p).strip()
    if inv.get("suffix"):
        name += ", " + inv["suffix"]
    return name


def representation_gate(data: dict, inventor_ids, applicant_id: str) -> dict:
    """Is a registered practitioner required for this combination?

    37 CFR 1.31(a), effective 2026-07-20:
      (1) a juristic entity must be represented;
      (2) so must an applicant (§ 1.42) with at least one party domiciled outside
          the United States or its territories.
    And § 1.42(a): with no 1.46 applicant named, the applicant IS all the joint
    inventors, so every inventor's domicile counts.
    """
    invs = [find(data, "inventors", i) for i in (inventor_ids or [])]
    invs = [i for i in invs if i]
    applicant = find(data, "applicants", applicant_id) if applicant_id else {}
    kind = applicant.get("kind") or "inventors"

    reasons, foreign = [], []
    if kind == "juristic":
        reasons.append("37 CFR 1.31(a)(1): the applicant %s is a juristic entity."
                       % (applicant.get("name") or "named"))
    if kind == "inventors":
        # The inventors collectively are the applicant, so each domicile is tested.
        for i in invs:
            country = ((i.get("residence") or {}).get("country") or "").upper()
            if country and country not in ("US", "USA", "PR", "VI", "GU", "AS", "MP"):
                foreign.append("%s (%s)" % (full_name(i), country))
    else:
        country = ((applicant.get("address") or {}).get("country") or "").upper()
        if kind != "juristic" and country and country not in ("US", "USA"):
            foreign.append("%s (%s)" % (applicant.get("name"), country))
    if foreign:
        reasons.append("37 CFR 1.31(a)(2): applicant domiciled outside the US: "
                       + ", ".join(foreign))

    missing = []
    for i in invs:
        m = i.get("mailing") or {}
        gaps = [label for label, key in (("street", "line1"), ("city", "city"),
                                         ("postcode", "postal"))
                if not (m.get(key) or "").strip()]
        # A postcode is not universal; only flag it for the US.
        if (m.get("country") or "").upper() not in ("US", "USA"):
            gaps = [g for g in gaps if g != "postcode"]
        if gaps:
            missing.append("%s: %s" % (full_name(i), ", ".join(gaps)))

    age_eligible = [full_name(i) for i in invs if i.get("age_65_plus")]
    return {
        "practitioner_required": bool(reasons),
        "reasons": reasons,
        "pro_se_ok": not reasons,
        "missing_inventor_data": missing,
        "age_petition_available": bool(age_eligible),
        "age_petition_inventors": age_eligible,
        "inventor_count": len(invs),
    }


def estimate_fees(entity_status: str = "small", total_claims: int = 20,
                  independent_claims: int = 3, sheets: int = 0,
                  multiple_dependent: bool = False, docx_spec: bool = True,
                  oath_with_application: bool = True) -> dict:
    """Line items for a utility nonprovisional, electronic filing."""
    ent = entity_status if entity_status in ENTITY_STATUSES else "small"
    lines = []

    def add(label, key, qty=1):
        row = FEES[key]
        unit = row.get(ent)
        if unit is None:
            return
        lines.append({"label": label, "code": row["code"], "qty": qty,
                      "unit": unit, "amount": unit * qty})

    if ent == "small":
        add("Basic filing fee, utility (electronic small entity)", "utility_basic_electronic")
    else:
        add("Basic filing fee, utility", "utility_basic_paper")
    add("Utility search fee", "utility_search")
    add("Utility examination fee", "utility_examination")
    if independent_claims > 3:
        add("Excess independent claims", "excess_independent", independent_claims - 3)
    if total_claims > 20:
        add("Excess claims over 20", "excess_claims", total_claims - 20)
    if multiple_dependent:
        add("Multiple dependent claim", "multiple_dependent")
    if sheets > 100:
        add("Application size fee, per 50 sheets over 100", "app_size_per_50",
            (sheets - 100 + 49) // 50)
    if not docx_spec:
        add("Non-DOCX specification surcharge", "non_docx_surcharge")
    if not oath_with_application:
        add("Surcharge, late oath or declaration", "late_oath_surcharge")

    return {"entity_status": ent, "lines": lines,
            "total": sum(x["amount"] for x in lines),
            "schedule_verified": FEE_SCHEDULE_VERIFIED,
            "note": ("Estimate. Patent Center computes the authoritative figure on the "
                     "Calculate fees step and that is what gets charged.")}


def slugify(text: str, fallback: str = "filing") -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", (text or "")).strip("-").lower()
    return (s[:40] or fallback)


def record_filing(entry: dict) -> dict:
    with _LOCK:
        data = load()
        entry = dict(entry)
        entry.setdefault("id", _uid("fil"))
        entry.setdefault("created", _now())
        data.setdefault("filings", []).insert(0, entry)
        data["filings"] = data["filings"][:200]
        save(data)
        return entry


def update_filing(filing_id: str, patch: dict) -> dict:
    with _LOCK:
        data = load()
        for f in data.get("filings") or []:
            if f.get("id") == filing_id:
                f.update(patch)
                save(data)
                return f
        return {}


def snapshot_for_agent(data: dict, inventor_ids, applicant_id: str,
                       correspondence_id: str, entity_status: str,
                       publication: str, docket: str, title: str) -> dict:
    """Everything the filing agent needs, resolved, with the gate already run."""
    invs = [find(data, "inventors", i) for i in (inventor_ids or [])]
    return {
        "title": title,
        "docket": docket,
        "entity_status": entity_status,
        "publication": publication,
        "inventors": [copy.deepcopy(i) for i in invs if i],
        "applicant": copy.deepcopy(find(data, "applicants", applicant_id)),
        "correspondence": copy.deepcopy(find(data, "correspondence", correspondence_id)),
        "payment": copy.deepcopy((data.get("payment") or [{}])[0]),
        "gate": representation_gate(data, inventor_ids, applicant_id),
        "fee_schedule_verified": FEE_SCHEDULE_VERIFIED,
    }


async def create_session(client, base_name: str, cwd: str, tries: int = 25):
    """Open a tmux session, working around a name that is already taken.

    The name is derived from the application or the title, so running the same
    demo twice, or filing twice against one application, collided and the dashboard
    answered "Session already exists". From the panel that looked like the button
    doing nothing at all. Retrying with a suffix also covers the race between two
    people pressing Submit at once.
    """
    last = ""
    for n in range(1, tries + 1):
        name = base_name if n == 1 else ("%s %d" % (base_name, n))[:60]
        resp = await client.post("/api/sessions/create", json={"name": name, "cwd": cwd})
        if resp.status_code < 400:
            created = (resp.json() or {}).get("name") or ""
            if created:
                return created, ""
            return "", "session create returned no name"
        last = resp.text[:300]
        if "already exists" not in last:
            return "", last
    return "", "could not find a free session name after %d tries: %s" % (tries, last)
