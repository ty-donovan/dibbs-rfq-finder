"""AMSC (Acquisition Method Suffix Code) letters, grouped by what they mean for a distributor.

The AMSC says *why* the government buys an item the way it does. AMC (the number: how it buys) is not
published on DIBBS; it lives in the NSN's FLIS record.
"""

AMSC_GROUPS: dict[str, dict] = {
    "open": {
        "label": "Open competition",
        "letters": "GZ",
        "hint": "Government has full data (G) or the item is commercial (Z). Alternates can compete.",
    },
    "approved": {
        "label": "Approved / qualified sources only",
        "letters": "CBTSKMNV",
        "hint": "Must quote an approved or qualified source's part.",
    },
    "no_data": {
        "label": "Government lacks data or rights",
        "letters": "DHPRAQUYL",
        "hint": "Stuck with the current approved sources.",
    },
}

AMSC_MEANINGS: dict[str, str] = {
    "G": "Full data, no restrictions; full and open competition",
    "Z": "Commercial / off-the-shelf item",
    "C": "Requires engineering source approval",
    "B": "Source-control drawing",
    "T": "Qualified Products List (QPL)",
    "S": "Militarily sensitive",
    "K": "Special castings or tooling",
    "M": "Special tooling",
    "N": "Special test or inspection facilities",
    "V": "High-reliability part",
    **{c: "Government lacks the data or rights to compete it" for c in "DHPRAQUYL"},
}


def amsc_group(letter: str | None) -> str | None:
    if not letter:
        return None
    for key, group in AMSC_GROUPS.items():
        if letter in group["letters"]:
            return key
    return "other"
