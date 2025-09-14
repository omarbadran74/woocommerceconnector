import frappe

def auto_reconcile_payments():
    invoices = frappe.get_all(
        "Purchase Invoice",
        filters={"docstatus": 1, "status": ["!=", "Paid"]},
        fields=["name", "outstanding_amount", "grand_total"]
    )

    for inv in invoices:
        payments = frappe.get_all(
            "Payment Entry Reference",
            filters={"reference_doctype": "Purchase Invoice", "reference_name": inv.name},
            fields=["allocated_amount"]
        )

        total_paid = sum([p.allocated_amount for p in payments])

        if total_paid >= inv.grand_total:
            frappe.db.set_value("Purchase Invoice", inv.name, "status", "Paid")
            frappe.db.commit()
            frappe.logger().info(f"✅ Marked {inv.name} as Paid (auto)")
