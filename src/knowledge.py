KEY_CONCEPTS = {
    "credit memo": "A negative billing document used to reduce what the customer owes after overbilling or a pricing error.",
    "make-good invoice": "A new invoice used to recover missing or underbilled revenue.",
    "plan amendment": "A change to the billing plan when the underlying agreement itself changed, such as amount, cadence, or entitlement.",
    "revenue leakage": "Revenue that should have been billed under the plan but was missed or underbilled.",
}


def answer_key_concept(message: str) -> str:
    text = message.lower().replace("make good", "make-good")
    matches = []
    for key, value in KEY_CONCEPTS.items():
        normalized = key.replace("make-good", "make-good")
        if normalized in text or key.split()[0] in text:
            matches.append(f"**{key.title()}**: {value}")
    return "\n\n".join(matches) if matches else "The Key Concepts registry contains Credit Memo, Make-Good Invoice, Plan Amendment, and Revenue Leakage."
