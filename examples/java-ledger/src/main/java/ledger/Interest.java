package ledger;

import java.math.BigDecimal;
import java.math.RoundingMode;
import java.time.LocalDate;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;

/**
 * Debit interest for overdrafts: per day by value date, method act/365. Interest above the
 * overdraft limit costs the excess overdraft rate. Every day is computed individually to 1/10000 €,
 * the sum is rounded half up to cents at the end.
 */
public final class Interest {

    public record Rates(BigDecimal debitPercent, BigDecimal overdraftPercent) {
    }

    public static final Rates DEFAULT = new Rates(new BigDecimal("9.5"), new BigDecimal("14.5"));

    private Interest() {
    }

    /** Daily balances (by value date) of an account from the opening balance; days without transactions take over the previous day. */
    public static List<BigDecimal> dailyBalances(BigDecimal opening, List<Transaction> txs, LocalDate from, LocalDate to) {
        List<Transaction> sorted = new ArrayList<>(txs);
        sorted.sort(Comparator.comparing(Transaction::valueDate).thenComparing(Transaction::id));
        List<BigDecimal> out = new ArrayList<>();
        BigDecimal balance = opening;
        int i = 0;
        for (LocalDate d = from; !d.isAfter(to); d = d.plusDays(1)) {
            while (i < sorted.size() && !sorted.get(i).valueDate().isAfter(d)) {
                balance = balance.add(sorted.get(i).amount());
                i++;
            }
            out.add(balance);
        }
        return out;
    }

    public static BigDecimal debitInterest(Account account, List<BigDecimal> balances, Rates rates) {
        BigDecimal sum = BigDecimal.ZERO;
        BigDecimal days = new BigDecimal("365");
        for (BigDecimal b : balances) {
            if (b.signum() >= 0) {
                continue;
            }
            BigDecimal debit = b.negate();
            BigDecimal withinLimit = debit.min(account.overdraftLimit());
            BigDecimal beyond = debit.subtract(withinLimit);
            BigDecimal day = withinLimit.multiply(rates.debitPercent())
                    .add(beyond.multiply(rates.overdraftPercent()))
                    .divide(Money.HUNDRED.multiply(days), 4, RoundingMode.HALF_UP);
            sum = sum.add(day);
        }
        return Money.of(sum);
    }

    /** Days above the overdraft limit. */
    public static int daysBeyondLimit(Account account, List<BigDecimal> balances) {
        int n = 0;
        for (BigDecimal b : balances) {
            if (b.negate().compareTo(account.overdraftLimit()) > 0) {
                n++;
            }
        }
        return n;
    }
}
