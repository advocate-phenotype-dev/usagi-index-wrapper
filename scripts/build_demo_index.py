#!/usr/bin/env python3
"""
Build a small demo search index + concept cache using ~80 real OMOP standard
concepts so the service can be tested without Athena vocabulary files.

Usage:
    python scripts/build_demo_index.py --db-path /tmp/usagi_demo.db
    python scripts/build_demo_index.py  # defaults to ./demo.db

The resulting SQLite file serves as both the search index and the concept
metadata cache.  Point the service at it with:

    USAGI_USAGI_DIR=.  USAGI_CONCEPT_DB_PATH=/tmp/usagi_demo.db  uvicorn ...
"""
import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from usagi_search.engine_native import NativeIndexBuilder

# ---------------------------------------------------------------------------
# ~80 well-known OMOP standard concepts + their common synonyms
# (concept_id, concept_name, domain_id, vocabulary_id, concept_class_id,
#  standard_concept, [synonyms...])
# ---------------------------------------------------------------------------
CONCEPTS = [
    # ── Conditions (SNOMED) ──────────────────────────────────────────────
    (201826, "Type 2 diabetes mellitus", "Condition", "SNOMED", "Clinical Finding", "S",
     ["T2DM", "Type II diabetes", "Non-insulin-dependent diabetes mellitus", "NIDDM",
      "Adult-onset diabetes"]),
    (4283893, "Type 1 diabetes mellitus", "Condition", "SNOMED", "Clinical Finding", "S",
     ["T1DM", "Type I diabetes", "Insulin-dependent diabetes mellitus", "IDDM",
      "Juvenile diabetes"]),
    (316866, "Hypertensive disorder, systemic arterial", "Condition", "SNOMED", "Clinical Finding", "S",
     ["Hypertension", "High blood pressure", "HTN", "Arterial hypertension"]),
    (4329847, "Myocardial infarction", "Condition", "SNOMED", "Clinical Finding", "S",
     ["Heart attack", "MI", "Acute MI", "Cardiac infarction", "Coronary infarction"]),
    (316139, "Heart failure", "Condition", "SNOMED", "Clinical Finding", "S",
     ["Congestive heart failure", "CHF", "Cardiac failure", "Heart failure NOS"]),
    (381316, "Atrial fibrillation", "Condition", "SNOMED", "Clinical Finding", "S",
     ["AF", "AFib", "A-fib", "Auricular fibrillation"]),
    (256723, "Asthma", "Condition", "SNOMED", "Clinical Finding", "S",
     ["Bronchial asthma", "Asthma NOS", "Reactive airway disease"]),
    (4064161, "Obesity", "Condition", "SNOMED", "Clinical Finding", "S",
     ["Overweight and obesity", "Morbid obesity", "Obese", "BMI over 30"]),
    (436665, "Major depressive disorder", "Condition", "SNOMED", "Clinical Finding", "S",
     ["MDD", "Clinical depression", "Unipolar depression", "Major depression"]),
    (318843, "Hypothyroidism", "Condition", "SNOMED", "Clinical Finding", "S",
     ["Underactive thyroid", "Hypothyroid", "Myxedema"]),
    (443454, "Cerebral infarction", "Condition", "SNOMED", "Clinical Finding", "S",
     ["Ischemic stroke", "Stroke", "CVA", "Cerebrovascular accident"]),
    (4223659, "Chronic obstructive pulmonary disease", "Condition", "SNOMED", "Clinical Finding", "S",
     ["COPD", "Emphysema", "Chronic bronchitis", "Pulmonary emphysema"]),
    (4287291, "Bacterial pneumonia", "Condition", "SNOMED", "Clinical Finding", "S",
     ["Pneumonia", "Lobar pneumonia", "Community-acquired pneumonia", "CAP"]),
    (197499, "Chronic kidney disease, stage 3", "Condition", "SNOMED", "Clinical Finding", "S",
     ["CKD stage 3", "Chronic renal failure stage 3"]),
    (46271022, "Chronic kidney disease", "Condition", "SNOMED", "Clinical Finding", "S",
     ["CKD", "Chronic renal failure", "Chronic renal insufficiency"]),
    (380378, "Sepsis", "Condition", "SNOMED", "Clinical Finding", "S",
     ["Bacteremia", "Blood poisoning", "Systemic infection"]),
    (317576, "Coronary artery disease", "Condition", "SNOMED", "Clinical Finding", "S",
     ["CAD", "Ischemic heart disease", "Coronary heart disease", "IHD"]),
    (436785, "Pulmonary embolism", "Condition", "SNOMED", "Clinical Finding", "S",
     ["PE", "Lung clot", "Pulmonary thromboembolism"]),
    (444094, "Deep vein thrombosis", "Condition", "SNOMED", "Clinical Finding", "S",
     ["DVT", "Deep venous thrombosis", "Thrombophlebitis"]),
    (432867, "Hyperlipidemia", "Condition", "SNOMED", "Clinical Finding", "S",
     ["High cholesterol", "Dyslipidemia", "Hypercholesterolemia", "Elevated lipids"]),
    (372328, "Anxiety disorder", "Condition", "SNOMED", "Clinical Finding", "S",
     ["Anxiety", "Generalized anxiety", "Anxiety NOS"]),
    (434610, "Bipolar disorder", "Condition", "SNOMED", "Clinical Finding", "S",
     ["Bipolar affective disorder", "Manic-depressive disorder", "Manic depression"]),
    (4108832, "Stroke", "Condition", "SNOMED", "Clinical Finding", "S",
     ["Cerebrovascular accident", "CVA", "Brain attack", "Apoplexy"]),
    (4195614, "Acute respiratory failure", "Condition", "SNOMED", "Clinical Finding", "S",
     ["ARF", "Respiratory failure", "ARDS"]),
    (192671, "Gastroesophageal reflux disease", "Condition", "SNOMED", "Clinical Finding", "S",
     ["GERD", "Acid reflux", "Heartburn", "Acid indigestion"]),

    # ── Drugs (RxNorm) ───────────────────────────────────────────────────
    (1310149, "Metformin", "Drug", "RxNorm", "Ingredient", "S",
     ["Glucophage", "Fortamet", "Glumetza", "Riomet"]),
    (1119510, "Lisinopril", "Drug", "RxNorm", "Ingredient", "S",
     ["Prinivil", "Zestril", "ACE inhibitor"]),
    (923645,  "Atorvastatin", "Drug", "RxNorm", "Ingredient", "S",
     ["Lipitor", "Statin", "HMG-CoA reductase inhibitor"]),
    (1539462, "Metoprolol succinate", "Drug", "RxNorm", "Ingredient", "S",
     ["Toprol XL", "Metoprolol", "Beta blocker"]),
    (1307046, "Warfarin", "Drug", "RxNorm", "Ingredient", "S",
     ["Coumadin", "Jantoven", "Blood thinner", "Anticoagulant"]),
    (1592988, "Apixaban", "Drug", "RxNorm", "Ingredient", "S",
     ["Eliquis", "Factor Xa inhibitor", "NOAC", "DOAC"]),
    (1549786, "Empagliflozin", "Drug", "RxNorm", "Ingredient", "S",
     ["Jardiance", "SGLT2 inhibitor"]),
    (40239216, "Liraglutide", "Drug", "RxNorm", "Ingredient", "S",
     ["Victoza", "Saxenda", "GLP-1 agonist"]),
    (19078461, "Semaglutide", "Drug", "RxNorm", "Ingredient", "S",
     ["Ozempic", "Wegovy", "Rybelsus", "GLP-1 receptor agonist"]),
    (1551803, "Amlodipine", "Drug", "RxNorm", "Ingredient", "S",
     ["Norvasc", "Calcium channel blocker", "CCB"]),
    (1346686, "Omeprazole", "Drug", "RxNorm", "Ingredient", "S",
     ["Prilosec", "Proton pump inhibitor", "PPI"]),
    (1125315, "Acetaminophen", "Drug", "RxNorm", "Ingredient", "S",
     ["Tylenol", "Paracetamol", "APAP"]),
    (1177480, "Ibuprofen", "Drug", "RxNorm", "Ingredient", "S",
     ["Advil", "Motrin", "NSAID", "Nonsteroidal anti-inflammatory"]),
    (1201620, "Amoxicillin", "Drug", "RxNorm", "Ingredient", "S",
     ["Amoxil", "Trimox", "Penicillin antibiotic"]),
    (1734104, "Albuterol", "Drug", "RxNorm", "Ingredient", "S",
     ["Salbutamol", "ProAir", "Ventolin", "Bronchodilator", "Rescue inhaler"]),
    (1110410, "Furosemide", "Drug", "RxNorm", "Ingredient", "S",
     ["Lasix", "Loop diuretic", "Water pill"]),
    (1308216, "Losartan", "Drug", "RxNorm", "Ingredient", "S",
     ["Cozaar", "ARB", "Angiotensin receptor blocker"]),
    (1518254, "Sitagliptin", "Drug", "RxNorm", "Ingredient", "S",
     ["Januvia", "DPP-4 inhibitor", "Gliptin"]),
    (1513876, "Rosuvastatin", "Drug", "RxNorm", "Ingredient", "S",
     ["Crestor", "Statin"]),
    (1364005, "Sertraline", "Drug", "RxNorm", "Ingredient", "S",
     ["Zoloft", "SSRI", "Antidepressant", "Selective serotonin reuptake inhibitor"]),

    # ── Measurements (LOINC) ────────────────────────────────────────────
    (3004249, "Systolic blood pressure", "Measurement", "LOINC", "Clinical Observation", "S",
     ["SBP", "Systolic BP", "Systolic pressure"]),
    (3012888, "Diastolic blood pressure", "Measurement", "LOINC", "Clinical Observation", "S",
     ["DBP", "Diastolic BP", "Diastolic pressure"]),
    (3013682, "Glucose [Mass/volume] in Blood", "Measurement", "LOINC", "Clinical Observation", "S",
     ["Blood glucose", "Blood sugar", "Fasting glucose", "Serum glucose"]),
    (3004410, "Hemoglobin A1c/Hemoglobin.total in Blood", "Measurement", "LOINC", "Clinical Observation", "S",
     ["HbA1c", "A1C", "Glycated hemoglobin", "Glycohemoglobin"]),
    (3016502, "Creatinine [Mass/volume] in Serum or Plasma", "Measurement", "LOINC", "Clinical Observation", "S",
     ["Creatinine", "Serum creatinine", "SCr"]),
    (3020891, "Body weight", "Measurement", "LOINC", "Clinical Observation", "S",
     ["Weight", "Patient weight", "Body mass"]),
    (3036277, "Body height", "Measurement", "LOINC", "Clinical Observation", "S",
     ["Height", "Patient height", "Stature"]),
    (3038553, "Body mass index", "Measurement", "LOINC", "Clinical Observation", "S",
     ["BMI", "Body mass index (BMI)", "Quetelet index"]),
    (3027018, "Heart rate", "Measurement", "LOINC", "Clinical Observation", "S",
     ["Pulse", "HR", "Pulse rate", "Heart beats per minute"]),
    (3024171, "Respiratory rate", "Measurement", "LOINC", "Clinical Observation", "S",
     ["RR", "Breathing rate", "Breaths per minute"]),
    (3020149, "Body temperature", "Measurement", "LOINC", "Clinical Observation", "S",
     ["Temperature", "Temp", "Core temperature", "Fever"]),
    (3016723, "Oxygen saturation in Arterial blood by Pulse oximetry", "Measurement", "LOINC", "Clinical Observation", "S",
     ["SpO2", "Pulse oximetry", "Oxygen saturation", "O2 sat"]),
    (3000963, "Hemoglobin [Mass/volume] in Blood", "Measurement", "LOINC", "Clinical Observation", "S",
     ["Hemoglobin", "Hgb", "Haemoglobin"]),
    (3010813, "Leukocytes [#/volume] in Blood", "Measurement", "LOINC", "Clinical Observation", "S",
     ["White blood cell count", "WBC", "Leukocyte count"]),
    (3024929, "Platelets [#/volume] in Blood", "Measurement", "LOINC", "Clinical Observation", "S",
     ["Platelet count", "PLT", "Thrombocyte count"]),

    # ── Procedures (SNOMED) ──────────────────────────────────────────────
    (4219816, "Colonoscopy", "Procedure", "SNOMED", "Procedure", "S",
     ["Lower GI endoscopy", "Colon scope", "Colorectal screening"]),
    (4080942, "Percutaneous coronary intervention", "Procedure", "SNOMED", "Procedure", "S",
     ["PCI", "Coronary angioplasty", "PTCA", "Cardiac catheterization"]),
    (4005823, "Coronary artery bypass graft", "Procedure", "SNOMED", "Procedure", "S",
     ["CABG", "Bypass surgery", "Open heart surgery", "Heart bypass"]),
    (4032243, "Kidney transplant", "Procedure", "SNOMED", "Procedure", "S",
     ["Renal transplant", "Kidney transplantation"]),
    (4141905, "Appendectomy", "Procedure", "SNOMED", "Procedure", "S",
     ["Appendix removal", "Surgical appendectomy"]),
    (4059173, "Total knee replacement", "Procedure", "SNOMED", "Procedure", "S",
     ["TKR", "Knee arthroplasty", "Total knee arthroplasty", "TKA"]),
    (4026673, "Total hip replacement", "Procedure", "SNOMED", "Procedure", "S",
     ["THR", "Hip arthroplasty", "Total hip arthroplasty", "THA"]),
    (40481531, "Hemodialysis", "Procedure", "SNOMED", "Procedure", "S",
     ["Dialysis", "Renal dialysis", "HD"]),

    # ── Observations ─────────────────────────────────────────────────────
    (4275495, "Tobacco smoking status", "Observation", "SNOMED", "Clinical Finding", "S",
     ["Smoking status", "Cigarette smoking", "Current smoker"]),
    (4239408, "Alcohol use", "Observation", "SNOMED", "Clinical Finding", "S",
     ["Alcohol consumption", "Drinking history", "Alcohol intake"]),
]


def build(db_path: str) -> None:
    builder = NativeIndexBuilder(db_path)
    builder.open()

    total_terms = 0
    for row in CONCEPTS:
        cid, name, domain, vocab, cls, std, synonyms = row
        builder.add_term(name, cid, domain, vocab, cls, std, "C")
        total_terms += 1
        for syn in synonyms:
            builder.add_term(syn, cid, domain, vocab, cls, std, "C")
            total_terms += 1

    builder.commit()
    builder.close()

    # Also populate the concept-name lookup table in the same file
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS concepts (
            concept_id       INTEGER PRIMARY KEY,
            concept_name     TEXT NOT NULL,
            domain_id        TEXT,
            vocabulary_id    TEXT,
            concept_class_id TEXT,
            standard_concept TEXT,
            concept_code     TEXT,
            valid_start_date TEXT,
            valid_end_date   TEXT,
            invalid_reason   TEXT
        )
    """)
    conn.executemany(
        "INSERT OR REPLACE INTO concepts VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            (cid, name, domain, vocab, cls, std, "", "19700101", "20991231", "")
            for cid, name, domain, vocab, cls, std, *_ in CONCEPTS
        ],
    )
    conn.commit()
    conn.close()

    print(f"Demo index built: {len(CONCEPTS)} concepts, {total_terms} indexed terms → {db_path}")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db-path", default="demo.db",
                   help="Output SQLite path (default: ./demo.db)")
    args = p.parse_args()
    build(args.db_path)


if __name__ == "__main__":
    main()
