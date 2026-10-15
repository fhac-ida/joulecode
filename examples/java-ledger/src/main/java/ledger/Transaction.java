package ledger;

import java.math.BigDecimal;
import java.time.LocalDate;

/**
 * Transaction on an account. Positive amounts are credits (incoming payments),
 * negative ones debits. The purpose often contains invoice numbers.
 */
public record Transaction(
        String id,
        String accountId,
        LocalDate bookingDate,
        LocalDate valueDate,
        BigDecimal amount,
        String currency,
        String counterpartyIban,
        String counterpartyName,
        String purpose,
        String customerId) {

    public Transaction {
        amount = Money.of(amount);
        purpose = purpose == null ? "" : purpose;
    }

    public boolean isCredit() {
        return amount.signum() > 0;
    }
}
