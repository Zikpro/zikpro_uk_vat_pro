"""P2-3 proof: Capital Goods Scheme 5/10-year interval adjustments (VAT Notice 706/2).
Unit-tests the interval calc (deterministic) + the schedule generator (dates + amounts).

    bench --site erpnext.zikpro.local execute zikpro_uk_vat.smoke.verify_cgs.run
"""

import frappe

from zikpro_uk_vat import cockpit as ck
from zikpro_uk_vat import vat_adjustment_schedule as vs

REF = "CGS-TEST-ASSET"


def _clean():
	for n in frappe.get_all("VAT Adjustment Schedule", filters={"reason": ["like", "CGS interval%"]}, pluck="name"):
		frappe.db.set_value("VAT Adjustment Schedule", n, "docstatus", 0, update_modified=False)
		frappe.delete_doc("VAT Adjustment Schedule", n, force=True, ignore_permissions=True)
	frappe.db.commit()


def run():
	results = []

	def check(label, cond, got=None):
		results.append(bool(cond))
		print(f"[{'PASS' if cond else 'FAIL'}] {label}" + ("" if cond else f"  (got {got})"), flush=True)

	# --- pure interval calc: annual slice × change in taxable-use % ---
	# £20,000 over 10 intervals = £2,000/yr; use 80% → 90% = +10% → +£200.
	check("interval calc: use rises 80→90% → +200", vs._cgs_interval_adjustment(20000, 10, 80, 90) == 200.0,
		  vs._cgs_interval_adjustment(20000, 10, 80, 90))
	check("interval calc: use falls 80→70% → -200", vs._cgs_interval_adjustment(20000, 10, 80, 70) == -200.0,
		  vs._cgs_interval_adjustment(20000, 10, 80, 70))
	check("interval calc: no change → 0", vs._cgs_interval_adjustment(20000, 10, 80, 80) == 0.0)
	# £50,000 over 5 intervals = £10,000/yr; 100% → 60% = -40% → -£4,000.
	check("interval calc: 5-yr, 100→60% → -4000", vs._cgs_interval_adjustment(50000, 5, 100, 60) == -4000.0,
		  vs._cgs_interval_adjustment(50000, 5, 100, 60))

	# --- generator: a row per KNOWN, non-zero interval on its annual anniversary ---
	# intervals 2 (use 90% → +200) and 3 (use 70% → -200) create rows; interval 4 (use 80% =
	# baseline → 0) creates none; unknown years create none.
	_clean()
	company = frappe.db.get_single_value("Global Defaults", "default_company")
	names = vs.generate_cgs_schedule(
		company=company, total_input_vat=20000, intervals=10, acquisition_date="2026-04-01",
		baseline_use_pct=80, interval_use_pcts={2: 90, 3: 70, 4: 80},
	)
	check("only known non-zero intervals create rows (2 & 3; 4 unchanged skipped)", len(names) == 2, len(names))
	rows = {r.cgs_interval_number: r for r in frappe.get_all(
		"VAT Adjustment Schedule", filters={"reason": ["like", "CGS interval%"]},
		fields=["name", "cgs_interval_number", "amount", "trigger_date", "schedule_type", "vat_box", "status"])}
	check("interval 2 amount = +200 (use 90%)", rows.get(2) and rows[2].amount == 200.0, rows.get(2) and rows[2].amount)
	check("interval 2 triggers on +1yr anniversary (2027-04-01)", rows.get(2) and str(rows[2].trigger_date) == "2027-04-01",
		  rows.get(2) and str(rows[2].trigger_date))
	check("interval 3 amount = -200 (use 70%)", rows.get(3) and rows[3].amount == -200.0, rows.get(3) and rows[3].amount)
	check("interval 3 triggers on +2yr (2028-04-01)", rows.get(3) and str(rows[3].trigger_date) == "2028-04-01",
		  rows.get(3) and str(rows[3].trigger_date))
	check("interval 4 (unchanged use) NOT created", 4 not in rows)
	check("all rows are Capital Goods Scheme / Box 4 / Pending", all(
		r.schedule_type == "Capital Goods Scheme" and r.vat_box == "Box 4" and r.status == "Pending" for r in rows.values()))

	# --- set_cgs_interval_use recomputes a still-Pending interval ---
	upd = vs.set_cgs_interval_use(rows[2].name, 60)  # 80→60% → 2000 × -20% = -400
	check("set_cgs_interval_use recomputes amount (60% → -400)", upd["amount"] == -400.0, upd["amount"])

	# --- 5/10 guard ---
	try:
		vs.generate_cgs_schedule(company=frappe.db.get_single_value("Global Defaults", "default_company"),
								 total_input_vat=1000, intervals=7, acquisition_date="2026-04-01", baseline_use_pct=50)
		check("intervals must be 5 or 10 (7 rejected)", False)
	except frappe.exceptions.ValidationError:
		check("intervals must be 5 or 10 (7 rejected)", True)

	_clean()
	print(f"\n=== CGS (P2-3) PROOF: {sum(results)}/{len(results)} passed ===", flush=True)
	return sum(results) == len(results)
