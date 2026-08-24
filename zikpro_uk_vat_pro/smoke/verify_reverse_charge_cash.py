"""P2-1 proof: under CASH accounting, a reverse-charge purchase is excluded from the cash
scheme (Notice 731 §6.2) — invoice-accounted even when UNPAID — and the DRC notional still
self-accounts into Box 1 and Box 4.

    bench --site erpnext.zikpro.local execute zikpro_uk_vat.smoke.verify_reverse_charge_cash.run
"""

import frappe

from zikpro_uk_vat import cockpit as ck

COMPANY = "Demo Company"
VAT_ACCOUNT = "VAT - DC"
RC_TMPL = "UK VAT RC Purchase Test - DC"
PERIOD = ("2027-03-01", "2027-03-31")
INV_DATE = "2027-03-20"
TAG = "RCCASH"


def _clean():
	for n in frappe.get_all("Purchase Invoice", filters={"bill_no": ["like", f"{TAG}%"]}, pluck="name"):
		d = frappe.get_doc("Purchase Invoice", n)
		if d.docstatus == 1:
			d.cancel()
		frappe.delete_doc("Purchase Invoice", n, force=True, ignore_permissions=True)
	frappe.db.commit()


def run():
	results = []

	def check(label, cond, got=None):
		results.append(bool(cond))
		extra = "" if cond else f"  (got {got})"
		print(f"[{'PASS' if cond else 'FAIL'}] {label}{extra}", flush=True)

	# RC classification template at 0% (no actual VAT) + treatment with notional_rate 20.
	if not frappe.db.exists("Item Tax Template", RC_TMPL):
		t = frappe.new_doc("Item Tax Template")
		t.title = "UK VAT RC Purchase Test"
		t.company = COMPANY
		t.append("taxes", {"tax_type": VAT_ACCOUNT, "tax_rate": 0})
		t.insert(ignore_permissions=True)
		frappe.db.commit()
	s = frappe.get_doc("VAT Settings", ck._connection()["settings"])
	if not ck.vat_accounts(ck.OUTPUT_VAT):
		s.append("vat_accounts", {"vat_type": ck.OUTPUT_VAT, "account": VAT_ACCOUNT})
	if not ck.vat_accounts(ck.INPUT_VAT):
		s.append("vat_accounts", {"vat_type": ck.INPUT_VAT, "account": VAT_ACCOUNT})
	row = [t for t in s.vat_treatments if t.item_tax_template == RC_TMPL]
	if row:
		row[0].vat_treatment = "Reverse charge (purchase)"; row[0].in_box_6_7 = 1; row[0].notional_rate = 20
	else:
		s.append("vat_treatments", {"item_tax_template": RC_TMPL, "vat_treatment": "Reverse charge (purchase)",
									"in_box_6_7": 1, "notional_rate": 20})
	s.save(ignore_permissions=True)
	orig_scheme = frappe.db.get_value("VAT Settings", s.name, "vat_accounting_scheme")
	frappe.db.set_value("VAT Settings", s.name, "vat_accounting_scheme", ck.CASH)
	frappe.db.commit()

	try:
		_run_checks(s, check)
	finally:
		# ALWAYS restore the scheme — otherwise a crash strands the site on Cash and every
		# accrual proof then fails on the wrong basis.
		_clean()
		frappe.db.set_value("VAT Settings", s.name, "vat_accounting_scheme", orig_scheme)
		frappe.db.commit()

	print(f"\n=== REVERSE-CHARGE CASH (P2-1) PROOF: {sum(results)}/{len(results)} passed ===", flush=True)
	return sum(results) == len(results)


def _run_checks(s, check):
	# An UNPAID reverse-charge purchase, net 1000.
	_clean()
	supplier = frappe.db.get_value("Supplier", {"disabled": 0}, "name")
	expense = frappe.db.get_value("Account", {"company": COMPANY, "is_group": 0, "root_type": "Expense"}, "name")
	pi = frappe.new_doc("Purchase Invoice")
	pi.company = COMPANY; pi.supplier = supplier; pi.set_posting_time = 1
	pi.posting_date = INV_DATE; pi.bill_no = f"{TAG}-{frappe.generate_hash(length=5)}"
	pi.taxes_and_charges = None
	pi.append("items", {"item_name": TAG, "description": TAG, "qty": 1, "rate": 1000,
						"expense_account": expense, "uom": "Nos", "item_tax_template": RC_TMPL})
	pi.insert(ignore_permissions=True)
	pi.submit()
	frappe.db.commit()

	fig_on = ck.get_return_figures(*PERIOD)
	check("basis is cash", fig_on.get("basis") == "cash", fig_on.get("basis"))
	# RC net is invoice-accounted despite being UNPAID (a normal unpaid purchase would be £0 on cash).
	check("Box 7 = 1000 (RC net invoice-accounted though unpaid)", round(float(fig_on["boxes"]["box7"]), 0) == 1000.0,
		  fig_on["boxes"]["box7"])
	check("Box 6 = 0 (RC purchase not a sale)", round(float(fig_on["boxes"]["box6"]), 0) == 0.0, fig_on["boxes"]["box6"])

	# Notional delta: turning it off drops Box 1 and Box 4 by 200 each.
	frappe.db.set_value("MTD VAT Treatment", {"parent": s.name, "item_tax_template": RC_TMPL}, "notional_rate", 0, update_modified=False)
	frappe.clear_cache(doctype="VAT Settings")
	fig_off = ck.get_return_figures(*PERIOD)
	frappe.db.set_value("MTD VAT Treatment", {"parent": s.name, "item_tax_template": RC_TMPL}, "notional_rate", 20, update_modified=False)
	frappe.clear_cache(doctype="VAT Settings")
	d1 = round(float(fig_on["boxes"]["box1"]) - float(fig_off["boxes"]["box1"]), 2)
	d4 = round(float(fig_on["boxes"]["box4"]) - float(fig_off["boxes"]["box4"]), 2)
	check("Box 1 += 200 notional on cash basis", d1 == 200.0, d1)
	check("Box 4 += 200 notional on cash basis", d4 == 200.0, d4)
