package ledger;

import java.math.BigDecimal;
import java.time.DayOfWeek;
import java.time.LocalDate;
import java.time.temporal.TemporalAdjusters;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

/**
 * Liquidity forecast: when are the open amounts expected to come in?
 *
 * <p>Expected payment date = invoice date + the customer's average payment duration
 * (without history: due date). If it lies in the past, tomorrow is assumed.
 * Result per calendar week (Monday as the key), only within the horizon.
 */
public final class Forecast {

    private Forecast() {
    }

    public static LocalDate expectedPayment(Invoice inv, Map<String, Double> avgDaysToPay, LocalDate today) {
        Double avg = avgDaysToPay.get(inv.customerId());
        LocalDate expected = avg == null ? inv.due() : inv.issued().plusDays(Math.round(avg));
        return expected.isAfter(today) ? expected : today.plusDays(1);
    }

    public static Map<LocalDate, BigDecimal> weekly(List<Invoice> invoices, Map<String, Double> avgDaysToPay,
            LocalDate today, int horizonWeeks) {
        Map<LocalDate, BigDecimal> out = new TreeMap<>();
        LocalDate firstMonday = today.with(TemporalAdjusters.previousOrSame(DayOfWeek.MONDAY));
        for (int w = 0; w < horizonWeeks; w++) {
            out.put(firstMonday.plusWeeks(w), Money.ZERO);
        }
        LocalDate end = firstMonday.plusWeeks(horizonWeeks);
        for (Invoice inv : invoices) {
            if (inv.isSettled()) {
                continue;
            }
            LocalDate when = expectedPayment(inv, avgDaysToPay, today);
            if (!when.isBefore(end)) {
                continue;
            }
            LocalDate week = when.with(TemporalAdjusters.previousOrSame(DayOfWeek.MONDAY));
            out.merge(week, inv.open(), BigDecimal::add);
        }
        return out;
    }

    /** Sum of the forecast. */
    public static BigDecimal total(Map<LocalDate, BigDecimal> weekly) {
        return weekly.values().stream().reduce(Money.ZERO, BigDecimal::add);
    }
}
