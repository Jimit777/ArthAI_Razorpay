"""
The vendor invoice auditor's engine. Pure Python, no model - see CLAUDE.md
section 2. Checks what a supplier billed against the rate the merchant
agreed to pay, the same shape as the settlement auditor applied one door
down: gateway fees against the merchant's own contract there, supplier
line-item prices against the merchant's own purchase terms here.
"""
