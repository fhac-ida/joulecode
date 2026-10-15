package ledger;

import java.math.BigDecimal;
import java.time.LocalDate;

/** Outgoing invoice. `paid` is the amount already assigned. */
public final class Invoice {

    private final String number;
    private final String customerId;
    private final LocalDate issued;
    private final LocalDate due;
    private final BigDecimal amount;
    private final String currency;
    private BigDecimal paid = Money.ZERO;

    public Invoice(String number, String customerId, LocalDate issued, LocalDate due, BigDecimal amount, String currency) {
        if (due.isBefore(issued)) {
            throw new IllegalArgumentException("invoice " + number + ": due before issue");
        }
        this.number = number;
        this.customerId = customerId;
        this.issued = issued;
        this.due = due;
        this.amount = Money.of(amount);
        this.currency = currency;
    }

    public String number() { return number; }
    public String customerId() { return customerId; }
    public LocalDate issued() { return issued; }
    public LocalDate due() { return due; }
    public BigDecimal amount() { return amount; }
    public String currency() { return currency; }
    public BigDecimal paid() { return paid; }

    public BigDecimal open() {
        return amount.subtract(paid);
    }

    public boolean isSettled() {
        return open().signum() <= 0;
    }

    /** Assign a payment; returns the part that was no longer needed (overpayment). */
    public BigDecimal apply(BigDecimal payment) {
        BigDecimal needed = open().max(Money.ZERO);
        BigDecimal used = payment.min(needed);
        paid = paid.add(used);
        return payment.subtract(used);
    }

    public long daysOverdue(LocalDate today) {
        return isSettled() || !today.isAfter(due) ? 0 : today.toEpochDay() - due.toEpochDay();
    }

    @Override
    public String toString() {
        return number + " (" + customerId + ", open " + Money.format(open()) + ")";
    }
}
