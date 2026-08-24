"""P2-4 proof: the Partial Exemption ANNUAL adjustment (standard method + de minimis,
VAT Notice 706). Unit-tests the pure calc with known figures — deterministic, no reliance on
the shared dev site's invoice data.

    bench --site erpnext.zikpro.local execute zikpro_uk_vat.smoke.verify_pe_annual.run
"""

import frappe

from zikpro_uk_vat import cockpit as ck


def run():
	results = []

	def check(label, cond, got=None):
		results.append(bool(cond))
		print(f"[{'PASS' if cond else 'FAIL'}] {label}" + ("" if cond else f"  (got {got})"), flush=True)

	# taxable 3,000 + exempt 7,000 (recovery 30%), input VAT 1,000, 12-month year.
	# exempt input tax = 1000 × 70% = 700, and 700 ≥ 50% of 1,000 → NOT de minimis.
	pe = ck._pe_annual_from_figures(box6=10000, box4=1000, exempt_net=7000, months=12)
	check("applicable", pe["applicable"] is True)
	check("recovery % = 30 (taxable/total, rounded UP)", pe["recovery_pct"] == 30, pe["recovery_pct"])
	check("exempt input tax = 700", pe["exempt_input_tax"] == 700.0, pe["exempt_input_tax"])
	check("NOT de minimis", pe["de_minimis"] is False, pe["de_minimis"])
	check("adjustment = -700 (Box 4 clawback)", pe["adjustment"] == -700.0, pe["adjustment"])

	# rounding UP (in a non-de-minimis case): taxable 4,001 / 10,000 = 40.01% → 41%.
	# exempt input = 1000 × 59% = 590 ≥ 50% of 1,000 → not de minimis, so the % isn't overridden.
	pe_r = ck._pe_annual_from_figures(box6=10000, box4=1000, exempt_net=5999, months=12)
	check("recovery % rounds UP (40.01 → 41)", pe_r["recovery_pct"] == 41, pe_r["recovery_pct"])

	# de minimis: exempt input tax under £625/mo AND under 50% of input tax → recover all.
	# exempt 500 / 10,500 → ~5% exempt; exempt input ≈ £48 → de minimis.
	pe_dm = ck._pe_annual_from_figures(box6=10500, box4=1000, exempt_net=500, months=12)
	check("de minimis → recover all, adjustment 0", pe_dm["de_minimis"] is True and pe_dm["adjustment"] == 0.0,
		  {"dm": pe_dm["de_minimis"], "adj": pe_dm["adjustment"]})

	# 50%-of-input-tax limb: even a small £-amount is NOT de minimis if it's ≥ 50% of input tax.
	# exempt 6,000 / 10,000 → recovery 40%; input VAT only £100 → exempt input £60 (< £7,500) BUT
	# £60 ≥ 50% of £100 → NOT de minimis.
	pe_lim = ck._pe_annual_from_figures(box6=10000, box4=100, exempt_net=6000, months=12)
	check("50%-limb: high exempt share is NOT de minimis despite small £", pe_lim["de_minimis"] is False, pe_lim)

	# no exempt supplies → not applicable, no adjustment.
	pe_none = ck._pe_annual_from_figures(box6=10000, box4=1000, exempt_net=0, months=12)
	check("no exempt supplies → not applicable", pe_none["applicable"] is False and pe_none["adjustment"] == 0.0)

	# The whitelisted end-to-end path runs against real figures without error.
	live = ck.pe_annual_adjustment("2026-04-01", "2026-09-30")
	check("full pe_annual_adjustment path returns a calc dict", isinstance(live, dict) and "adjustment" in live)

	print(f"\n=== PE ANNUAL (P2-4) PROOF: {sum(results)}/{len(results)} passed ===", flush=True)
	return sum(results) == len(results)
