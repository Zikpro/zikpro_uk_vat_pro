"""Pro engines — the PREMIUM calculations the free base delegates to via cockpit._pro().

This module exists ONLY in the private Pro add-on. When installed, base cockpit's
`_pro("<name>", default, …)` finds these and uses them; without Pro it returns the
default (no reverse charge, no year-end adjustments). Shared helpers (vat_treatments,
PURCHASE_INVOICE, compute_boxes, …) are imported from the base app.
"""

import frappe

from zikpro_uk_vat import cockpit as c


# --- Domestic Reverse Charge (VAT Notice 735 / CIS) -------------------------
def construction_reverse_charge_templates(side=None):
	"""Which item tax templates are a construction reverse-charge supply. `side`:
	None → any; "purchase"/"sale" → that side. Classification lives on the VAT Settings
	treatment row (vat_treatment starts with 'Reverse charge')."""
	want = "Reverse charge" if side is None else f"Reverse charge ({side})"
	match = (lambda vt: vt.startswith(want)) if side is None else (lambda vt: vt == want)
	return {
		t.item_tax_template for t in c.vat_treatments()
		if t.item_tax_template and match(str(t.vat_treatment or ""))
	}


def reverse_charge_invoice_names(ref_doctype, from_date, to_date, company):
	"""Submitted period invoices carrying a reverse-charge line (Notice 731 §6.2: RC is
	always invoice-accounted under cash accounting)."""
	rc_templates = construction_reverse_charge_templates()
	if not rc_templates:
		return set()
	parents = {
		r.parent for r in frappe.get_all(
			f"{ref_doctype} Item",
			filters={"parenttype": ref_doctype, "item_tax_template": ["in", list(rc_templates)]},
			fields=["parent"],
		)
	}
	if not parents:
		return set()
	f = {"name": ["in", list(parents)], "docstatus": 1, "posting_date": ["between", [from_date, to_date]]}
	if company:
		f["company"] = company
	return set(frappe.get_all(ref_doctype, filters=f, pluck="name"))


def reverse_charge_notional(purchase_invoice_names):
	"""Customer-side notional VAT on reverse-charge purchases = net × the treatment's
	notional_rate, self-accounted into Box 1 and Box 4 by compute_boxes."""
	if not purchase_invoice_names:
		return 0.0
	rate = {
		t.item_tax_template: float(t.notional_rate or 0) / 100.0
		for t in c.vat_treatments()
		if t.vat_treatment == "Reverse charge (purchase)" and t.item_tax_template
	}
	if not rate:
		return 0.0
	total = 0.0
	for line in frappe.get_all(
		f"{c.PURCHASE_INVOICE} Item",
		filters={
			"parenttype": c.PURCHASE_INVOICE,
			"parent": ["in", purchase_invoice_names],
			"item_tax_template": ["in", list(rate)],
		},
		fields=["item_tax_template", "base_net_amount"],
	):
		total += float(line.base_net_amount or 0) * rate.get(line.item_tax_template, 0.0)
	return total


# --- Partial Exemption annual (VAT Notice 706) ------------------------------
def pe_annual_from_figures(box6, box4, exempt_net, months):
	"""Pure standard-method + de-minimis calc (no DB)."""
	import math
	taxable_net = box6 - exempt_net
	total = taxable_net + exempt_net
	base = {"input_vat": c._r2(box4), "taxable_supplies": c._r0(taxable_net), "exempt_supplies": c._r0(exempt_net)}
	if total <= 0 or exempt_net <= 0 or box4 <= 0:
		return {**base, "applicable": False, "recovery_pct": 100, "adjustment": 0.0}
	recovery_pct = min(100, math.ceil(taxable_net / total * 100))
	recoverable = round(box4 * recovery_pct / 100.0, 2)
	exempt_input = round(box4 - recoverable, 2)
	de_minimis = exempt_input < (625.0 * months) and exempt_input < 0.5 * box4
	if de_minimis:
		return {**base, "applicable": True, "de_minimis": True, "recovery_pct": 100,
				"recoverable": c._r2(box4), "exempt_input_tax": c._r2(exempt_input), "adjustment": 0.0}
	return {**base, "applicable": True, "de_minimis": False, "recovery_pct": recovery_pct,
			"recoverable": c._r2(recoverable), "exempt_input_tax": c._r2(exempt_input),
			"adjustment": c._r2(-exempt_input)}


def pe_annual_adjustment(from_date, to_date):
	"""Standard-method Partial Exemption annual adjustment for a VAT year."""
	from frappe.utils import getdate
	fig = c._accrual_figures(from_date, to_date)
	boxes = fig["boxes"]
	box6 = float(boxes.get("box6", 0) or 0)
	box4 = float(boxes.get("box4", 0) or 0)
	sales_names = [s["name"] for s in fig["sales"]]
	exempt_templates = {t.item_tax_template for t in c.vat_treatments() if t.vat_treatment == "Exempt"}
	exempt_net = 0.0
	if exempt_templates and sales_names:
		exempt_net = sum(
			c._excluded_net_by_parent(f"{c.SALES_INVOICE} Item", c.SALES_INVOICE, sales_names, exempt_templates).values()
		)
	months = max(1, (getdate(to_date).year - getdate(from_date).year) * 12
				 + (getdate(to_date).month - getdate(from_date).month) + 1)
	res = pe_annual_from_figures(box6, box4, exempt_net, months)
	res.update({"from": from_date, "to": to_date})
	return res


# --- Capital Goods Scheme (VAT Notice 706/2) --------------------------------
_SCHEDULE = "VAT Adjustment Schedule"


def _cgs_interval_adjustment(total_input_vat, intervals, baseline_use_pct, interval_use_pct):
	from frappe.utils import flt
	if not intervals:
		return 0.0
	annual_slice = flt(total_input_vat) / int(intervals)
	return round(annual_slice * (flt(interval_use_pct) - flt(baseline_use_pct)) / 100.0, 2)


def generate_cgs_schedule(company, total_input_vat, intervals, acquisition_date, baseline_use_pct,
						  reference_doctype=None, reference_name=None, interval_use_pcts=None):
	from frappe.utils import add_years, getdate, flt
	if int(intervals) not in (5, 10):
		frappe.throw(frappe._("Capital Goods Scheme intervals must be 5 or 10 (Notice 706/2)."))
	interval_use_pcts = {int(k): v for k, v in (interval_use_pcts or {}).items()}
	created = []
	for n in range(2, int(intervals) + 1):
		if n not in interval_use_pcts:
			continue
		use_pct = flt(interval_use_pcts[n])
		amount = _cgs_interval_adjustment(total_input_vat, intervals, baseline_use_pct, use_pct)
		if amount == 0:
			continue
		s = frappe.new_doc(_SCHEDULE)
		s.schedule_type = "Capital Goods Scheme"
		s.company = company
		s.trigger_date = add_years(getdate(acquisition_date), n - 1)
		s.vat_box = "Box 4"
		s.amount = amount
		s.reason = f"CGS interval {n} of {intervals}"
		s.notice_ref = "Notice 706/2"
		if reference_doctype and reference_name:
			s.reference_doctype = reference_doctype
			s.reference_name = reference_name
		s.cgs_total_input_vat = flt(total_input_vat)
		s.cgs_intervals = int(intervals)
		s.cgs_interval_number = n
		s.cgs_baseline_use_pct = flt(baseline_use_pct)
		s.cgs_interval_use_pct = use_pct
		s.insert(ignore_permissions=True)
		s.flags.ignore_permissions = True
		s.submit()
		created.append(s.name)
	frappe.db.commit()
	return created


def set_cgs_interval_use(schedule_name, interval_use_pct):
	from frappe.utils import flt
	s = frappe.get_doc(_SCHEDULE, schedule_name)
	if s.status != "Pending":
		frappe.throw(frappe._("This CGS interval has already been generated and cannot be re-computed."))
	amount = _cgs_interval_adjustment(s.cgs_total_input_vat, s.cgs_intervals, s.cgs_baseline_use_pct, interval_use_pct)
	s.db_set("cgs_interval_use_pct", flt(interval_use_pct), update_modified=False)
	s.db_set("amount", amount, update_modified=False)
	frappe.db.commit()
	return {"name": schedule_name, "interval_use_pct": flt(interval_use_pct), "amount": amount}


# --- cockpit-facing Pro methods (base delegates via _pro) -------------------
def cgs_preview(total_input_vat, intervals, baseline_use_pct, interval_use_pct):
	return {"amount": _cgs_interval_adjustment(
		float(total_input_vat), int(intervals), float(baseline_use_pct), float(interval_use_pct))}


def create_cgs_schedule(company, total_input_vat, intervals, acquisition_date, baseline_use_pct,
						reference_doctype=None, reference_name=None):
	if not company or not frappe.db.exists(c.VAT_SETTINGS, {"company": company}):
		frappe.throw(
			frappe._("VAT is not configured for the selected company, or you do not have access to it."),
			frappe.PermissionError,
		)
	names = generate_cgs_schedule(company, float(total_input_vat), int(intervals), acquisition_date,
								  float(baseline_use_pct), reference_doctype, reference_name)
	return {"ok": True, "schedules": names, "count": len(names)}


def compute_schedule_amount(schedule_type, total_vat=None, intervals=None, baseline_pct=None,
							interval_pct=None, residual_vat=None, annual_pct=None, provisional_pct=None):
	try:
		if schedule_type == "Capital Goods Scheme":
			total, n = float(total_vat), int(intervals)
			base, use = float(baseline_pct), float(interval_pct)
			if n <= 0:
				return {"ok": False, "message": "Intervals must be 5 or 10."}
			amount = round((total / n) * (use - base) / 100.0, 2)
			note = f"({total:g} / {n}) x ({use:g}% - {base:g}%) = {amount}"
		elif schedule_type == "Partial Exemption Annual":
			res, ann, prov = float(residual_vat), float(annual_pct), float(provisional_pct)
			amount = round(res * (ann - prov) / 100.0, 2)
			note = f"{res:g} x ({ann:g}% - {prov:g}%) = {amount}"
		else:
			return {"ok": False, "message": "No calculator for this schedule type."}
	except (TypeError, ValueError):
		return {"ok": False, "message": "Enter valid numbers for the calculator."}
	return {"ok": True, "amount": amount, "note": note, "vat_box": "Box 4"}
