package ledger;

import java.math.BigDecimal;

/** Bank account of a customer (current account). The overdraft limit is given as a positive number. */
public record Account(String id, String iban, String owner, String currency, BigDecimal overdraftLimit) {

    public Account {
        if (!Iban.isValid(iban)) {
            throw new IllegalArgumentException("invalid IBAN " + iban + " for account " + id);
        }
        overdraftLimit = Money.of(overdraftLimit);
    }
}
