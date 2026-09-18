"""Builds a small PDF that mimics the real AHCCCS FFS chapter layout
(validated against FFS Chapter 10 rev 04/29/2026):
  header line per page  "04/29/2026 CHAPTER 10 INDIVIDUAL PRACTITIONER SERVICES <n> | <N>"
  ALL-CAPS unnumbered headings, word-number limits, REVISION DATES line."""
import pymupdf as fitz

HEADER = "04/29/2026 CHAPTER 10 INDIVIDUAL PRACTITIONER SERVICES {n} | {N}"

PAGES = [
    [
        ("REVISION DATES: 04/29/2026; 05/02/2024; 05/31/2023; 08/23/2022", 10, False),
        ("GENERAL INFORMATION", 12, True),
        ("This chapter describes billing requirements for individual practitioners who bill "
         "AHCCCS on the CMS 1500 claim form. Providers should also review Chapter 4, General Billing Rules.", 10, False),
        ("CORRECT CODING INITIATIVE", 12, True),
        ("AHCCCS follows Medicare's Correct Coding Initiative (CCI) policy and performs CCI edits "
         "and audits on Fee-For-Service claims for the same provider, same member, and same date of service.", 10, False),
        ("Modifier 59 cannot be billed with evaluation and management codes (99201-99499) or "
         "radiation therapy codes (77261-77499).", 10, False),
    ],
    [
        ("COMMUNITY HEALTH WORKER SERVICES", 12, True),
        ("Community Health Worker (CHW) and Community Health Representative (CHR) services are covered "
         "when provided by a registered CHW/CHR. Claims can be submitted for a maximum of four units per "
         "day, up to 24 units per month per member (codes 98960, 98961, 98962). These codes cannot be "
         "billed together on the same day for the same member.", 10, False),
        ("FAMILY PLANNING SERVICES", 12, True),
        ("Do not bill for CPT codes: 11975 - Insertion, implantable contraceptive capsules; and 11977 - "
         "Removal with reinsertion. Norplant insertion is no longer an AHCCCS-covered service because the "
         "manufacturer is no longer distributing it.", 10, False),
        ("ANESTHESIA SERVICES", 12, True),
        ("Providers may bill for a maximum of 180 minutes (three hours) for ASA code 01967. Anesthesia "
         "code 00938 (insertion of penile prosthesis) is not a covered service.", 10, False),
    ],
    [
        ("WELL EXAMS", 12, True),
        ("Well exams for adults 21 years of age and older are not covered. Effective 10/1/2013 well visits "
         "and well exam coverage will be re-instated for members 21 years of age and older.", 10, False),
        ("PROVIDER RESPONSIBILITIES", 12, True),
        ("Providers are encouraged to register for the AHCCCS email notification system and to review "
         "Claims Clues for updates. Questions may be directed to the Claims Customer Service unit.", 10, False),
    ],
]


def build(path: str) -> str:
    doc = fitz.open()
    N = len(PAGES)
    for n, blocks in enumerate(PAGES, 1):
        page = doc.new_page(width=612, height=792)
        y = 40
        page.insert_text((40, y), HEADER.format(n=n, N=N), fontsize=8, fontname="helv")
        y += 30
        for text, size, bold in blocks:
            font = "hebo" if bold else "helv"
            rect = fitz.Rect(40, y, 572, y + 300)
            used = page.insert_textbox(rect, text, fontsize=size, fontname=font, align=0)
            # insert_textbox returns leftover space; estimate height used
            lines = max(1, int(len(text) * size * 0.5 / 532) + 1)
            y += lines * (size + 4) + 10
        page.insert_text((40, 770), HEADER.format(n=n, N=N), fontsize=8, fontname="helv")
    doc.save(path)
    doc.close()
    return path
