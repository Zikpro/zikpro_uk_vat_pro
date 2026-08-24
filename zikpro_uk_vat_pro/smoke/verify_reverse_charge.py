"""P0-4 / XP-1 proof: Domestic Reverse Charge (Notice 735). The CUSTOMER of a
reverse-charge purchase self-accounts the notional VAT into Box 1 AND Box 4, with the net
in Box 7 and NOTHING in Box 6.

    bench --site erpnext.zikpro.local execute zikpro_uk_vat.smoke.verify_reverse_charge.run
"""

import frappe

from zikpro_uk_vat import cockpit as ck

COMPANY = "Demo Company"
VAT_ACCOUNT = "VAT - DC"
RC_TMPL_TITLE = "UK VAT RC Purchase Test"
PERIOD = ("2027-03-01", "2027-03-31")
INV_DATE = "2027-03-15"
TAG = "RCTEST"


def _rc_template():
	full = f"{RC_TMPL_TITLE} - DC"
	if not frappe.db.exists("Item Tax Template", full):
		t = frappe.new_doc("Item Tax Template")
		t.title = RC_TMPL_TITLE
		t.company = COMPANY
		t.append("taxes", {"tax_type": VAT_ACCOUNT, "tax_rate": 0})
		t.insert(ignore_permissions=True)
		frappe.db.commit()
	return full


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

	# 0. Pure unit: compute_boxes puts the notional in BOTH box1 and box4.
	b = ck.compute_boxes([], [], reverse_charge={"vat": 200})
	check("compute_boxes: rc notional -> box1=200", b["box1"] == 200, b["box1"])
	check("compute_boxes: rc notional -> box4=200", b["box4"] == 200, b["box4"])
	check("compute_boxes: box5 nets to 0 (nil cash effect)", b["box5"] == 0, b["box5"])

	# Setup: VAT accounts mapped + RC-purchase treatment classified (append, don't clobber); accrual.
	rc_tmpl = _rc_template()
	s = frappe.get_doc("VAT Settings", ck._connection()["settings"])
	if not ck.vat_accounts(ck.OUTPUT_VAT):
		s.append("vat_accounts", {"vat_type": ck.OUTPUT_VAT, "account": VAT_ACCOUNT})
	if not ck.vat_accounts(ck.INPUT_VAT):
		s.append("vat_accounts", {"vat_type": ck.INPUT_VAT, "account": VAT_ACCOUNT})
	existing = [t for t in s.vat_treatments if t.item_tax_template == rc_tmpl]
	if existing:
		existing[0].vat_treatment = "Reverse charge (purchase)"
		existing[0].in_box_6_7 = 1
		existing[0].notional_rate = 20
	else:
		s.append("vat_treatments", {"item_tax_template": rc_tmpl,
									"vat_treatment": "Reverse charge (purchase)",
									"in_box_6_7": 1, "notional_rate": 20})
	s.save(ignore_permissions=True)
	frappe.db.set_value("VAT Settings", s.name, "vat_accounting_scheme", ck.ACCRUAL)
	frappe.db.commit()

	# A reverse-charge PURCHASE: net 1000, the RC template on the line, NO actual VAT tax line.
	_clean()
	company = COMPANY
	supplier = frappe.db.get_value("Supplier", {"disabled": 0}, "name")
	expense = frappe.db.get_value("Account", {"company": company, "is_group": 0, "root_type": "Expense"}, "name")
	pi = frappe.new_doc("Purchase Invoice")
	pi.company = company
	pi.supplier = supplier
	pi.set_posting_time = 1
	pi.posting_date = INV_DATE
	pi.bill_no = f"{TAG}-{frappe.generate_hash(length=5)}"
	pi.taxes_and_charges = None
	pi.append("items", {"item_name": TAG, "description": TAG, "qty": 1, "rate": 1000,
						"expense_account": expense, "uom": "Nos", "item_tax_template": rc_tmpl})
	pi.insert(ignore_permissions=True)
	pi.submit()
	frappe.db.commit()

	# Direct: the notional = net × the treatment's notional rate, read independently of any
	# actual VAT on the invoice (that's the whole point — the customer self-accounts).
	check("_reverse_charge_notional = net 1000 × 20% = 200",
		  round(ck._reverse_charge_notional([pi.name]), 2) == 200.0, ck._reverse_charge_notional([pi.name]))

	# Integration DELTA: turning the notional on adds exactly +200 to Box 1 AND Box 4, and
	# leaves Box 6/7 untouched — isolating the engine's DRC contribution from whatever actual
	# VAT the demo company's default tax template happened to post on the invoice.
	fig_on = ck.get_return_figures(*PERIOD)
	frappe.db.set_value("MTD VAT Treatment",
		{"parent": s.name, "item_tax_template": rc_tmpl}, "notional_rate", 0, update_modified=False)
	frappe.clear_cache(doctype="VAT Settings")
	fig_off = ck.get_return_figures(*PERIOD)
	# restore
	frappe.db.set_value("MTD VAT Treatment",
		{"parent": s.name, "item_tax_template": rc_tmpl}, "notional_rate", 20, update_modified=False)
	frappe.clear_cache(doctype="VAT Settings")

	d = lambda k: round(float(fig_on["boxes"][k]) - float(fig_off["boxes"][k]), 2)
	check("Box 1 += 200 notional (customer self-accounts output)", d("box1") == 200.0, d("box1"))
	check("Box 4 += 200 notional (customer reclaims input)", d("box4") == 200.0, d("box4"))
	check("Box 6 unchanged (RC purchase is NOT the customer's sale)", d("box6") == 0.0, d("box6"))
	check("Box 7 unchanged by the notional (net already there as a purchase)", d("box7") == 0.0, d("box7"))

	_clean()
	print(f"\n=== REVERSE-CHARGE (P0-4/XP-1) PROOF: {sum(results)}/{len(results)} passed ===", flush=True)
	return sum(results) == len(results)
