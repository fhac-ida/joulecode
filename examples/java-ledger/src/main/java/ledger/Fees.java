package ledger;

import java.math.BigDecimal;
import java.time.YearMonth;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

/**
 * Account fees per month: base price plus a per-item fee, the first items free,
 * credits referring to an invoice free of charge. Result rounded to cents (HALF_UP).
 */
public final class Fees {

    public record Tariff(BigDecimal base, BigDecimal perItem, int freeItems) {
    }

    public static final Tariff BUSINESS = new Tariff(new BigDecimal("9.90"), new BigDecimal("0.35"), 20);

    private Fees() {
    }

    /** Fee per account and month. */
    public static Map<String, Map<YearMonth, BigDecimal>> monthly(List<Transaction> transactions, Tariff tariff) {
        Map<String, Map<YearMonth, Integer>> items = new TreeMap<>();
        for (Transaction tx : transactions) {
            if (tx.isCredit() && !Reconciler.extractReferences(tx.purpose()).isEmpty()) {
                continue;
            }
            items.computeIfAbsent(tx.accountId(), k -> new TreeMap<>())
                    .merge(YearMonth.from(tx.bookingDate()), 1, Integer::sum);
        }
        Map<String, Map<YearMonth, BigDecimal>> out = new TreeMap<>();
        items.forEach((account, months) -> {
            Map<YearMonth, BigDecimal> fees = new TreeMap<>();
            months.forEach((month, count) -> {
                int billable = Math.max(0, count - tariff.freeItems());
                fees.put(month, Money.of(tariff.base().add(tariff.perItem().multiply(BigDecimal.valueOf(billable)))));
            });
            out.put(account, fees);
        });
        return out;
    }

    /** Sum of all fees. */
    public static BigDecimal total(Map<String, Map<YearMonth, BigDecimal>> fees) {
        BigDecimal sum = Money.ZERO;
        for (Map<YearMonth, BigDecimal> months : fees.values()) {
            for (BigDecimal v : months.values()) {
                sum = sum.add(v);
            }
        }
        return sum;
    }
}
