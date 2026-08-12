# Contracting Playbook

Internal thresholds for inbound supplier agreements. This is the knowledge the
model cannot derive from a contract by reading it: nothing in a document says
what *this company* is willing to accept. Deviations do not block a contract --
they route it to Legal with the section cited.

Synthetic. No real company's policy is reproduced here.

---

## §1.1 Payment terms

Undisputed invoices are payable within **45 days** of receipt or later. Terms
shorter than 45 days weaken our working capital position and require Treasury
sign-off; terms longer than 90 days are not acceptable to most suppliers and
usually signal a drafting error.

Acceptable range: 45 to 90 days. Outside it, route to review.

## §1.2 Currency and amounts

Amounts must state an ISO 4217 currency. A figure with no currency is a
deviation regardless of size.

---

## §2.1 Initial term

Initial terms of **12 to 36 months** are standard. Anything beyond 36 months
requires Procurement approval, because it outlives our budgeting horizon.

## §2.2 Automatic renewal

**Automatic renewal is not accepted.** Renewal must require a positive written
act by both parties. An evergreen clause is the single most common source of
unintended spend, and it is a deviation even when the notice period is generous.

## §2.3 Termination for convenience

Either party must be able to terminate for convenience on **no more than 90
days** written notice. A longer notice period, or the absence of any
termination-for-convenience right, is a deviation.

---

## §3.1 Liability cap

The supplier's aggregate liability must be capped at **no less than EUR
250,000**, or the equivalent in the contract currency. A lower cap is a
deviation.

## §3.2 Uncapped and excluded liability

A contract that **excludes liability entirely**, or states no cap at all, is a
deviation and must not be auto-approved. Wording such as "neither party shall be
liable for any loss whatsoever" falls here even where death and personal injury
are carved out.

## §3.3 Indemnities

Uncapped indemnities in our favour are acceptable. Uncapped indemnities *given*
by us are a deviation.

---

## §4.1 Governing law

Acceptable jurisdictions are **Bulgaria, Germany, Austria, the Netherlands and
England & Wales**. Any other governing law is a deviation, because we hold no
retained counsel able to advise on it.

Offshore jurisdictions -- the Cayman Islands, the British Virgin Islands,
Panama, Seychelles -- are a deviation of elevated severity and additionally
trigger a counterparty due-diligence check.

## §4.2 Dispute resolution

Litigation in the courts of the governing jurisdiction is standard. Arbitration
is acceptable where the seat is in an acceptable jurisdiction under §4.1.

---

## §5.1 Data protection

Where the supplier processes personal data on our behalf, a **Data Processing
Agreement is mandatory**. Its absence is a deviation and cannot be waived by
the contract owner.

Suppliers in the `data_analytics`, `it_services` and `professional_services`
categories are presumed to process personal data unless the contract says
otherwise.

## §5.2 Sub-processing

Sub-processors must be disclosed. A general authorisation with no notification
duty is a deviation.

---

## §6.1 Confidentiality

Mutual confidentiality with a survival period of **three to five years** is
standard. A one-sided obligation binding only us is a deviation.

## §6.2 Publicity

The supplier may not name us as a customer without written consent. A clause
granting general marketing rights is a deviation.

---

## §7.1 Counterparty status

A supplier whose registry status is **suspended** must never be auto-approved,
whatever the commercial terms. Route to review citing the suspension.

## §7.2 Unknown counterparties

A counterparty that cannot be resolved to a registry entry above the matching
threshold is a deviation in itself. It may be a genuine new supplier, or it may
be a subsidiary, a renamed entity, or an impersonation -- and none of those can
be told apart from the contract alone.
