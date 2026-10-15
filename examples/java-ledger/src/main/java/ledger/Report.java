package ledger;

import java.math.BigDecimal;
import java.time.LocalDate;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Open items, dunning and closing report of the reconciliation.
 *
 * <p>Dunning levels by days overdue: from 14 days level 1, from 28 level 2, from 42 level 3.
 * Dunning fee per level 0 / 5 / 10 €.
 */
public final class Report {

    public record Dunning(String invoice, String customer, int level, long days, BigDecimal open, BigDecimal fee) {
    }

    private Report() {
    }

    public static int dunningLevel(long daysOverdue) {
        if (daysOverdue >= 42) {
            return 3;
        }
        if (daysOverdue >= 28) {
            return 2;
        }
        return daysOverdue >= 14 ? 1 : 0;
    }

    static BigDecimal dunningFee(int level) {
        return switch (level) {
            case 2 -> new BigDecimal("5.00");
            case 3 -> new BigDecimal("10.00");
            default -> Money.ZERO;
        };
    }

    /** Dunning list: highest level first, then largest open amount, then invoice number. */
    public static List<Dunning> dunningList(List<Invoice> invoices, LocalDate today) {
        List<Dunning> out = new ArrayList<>();
        for (Invoice inv : invoices) {
            long days = inv.daysOverdue(today);
            int level = dunningLevel(days);
            if (level > 0) {
                out.add(new Dunning(inv.number(), inv.customerId(), level, days, inv.open(), dunningFee(level)));
            }
        }
        out.sort(Comparator.comparingInt(Dunning::level).reversed()
                .thenComparing(Dunning::open, Comparator.reverseOrder())
                .thenComparing(Dunning::invoice));
        return out;
    }

    /** Open items by age (days since due date): not due, 1–30, 31–60, 61–90, > 90. */
    public static Map<String, BigDecimal> aging(List<Invoice> invoices, LocalDate today) {
        Map<String, BigDecimal> out = new LinkedHashMap<>();
        for (String k : List.of("not due", "1-30", "31-60", "61-90", ">90")) {
            out.put(k, Money.ZERO);
        }
        for (Invoice inv : invoices) {
            if (inv.isSettled()) {
                continue;
            }
            long d = today.toEpochDay() - inv.due().toEpochDay();
            String key = d <= 0 ? "not due" : d <= 30 ? "1-30" : d <= 60 ? "31-60" : d <= 90 ? "61-90" : ">90";
            out.merge(key, inv.open(), BigDecimal::add);
        }
        return out;
    }

    /** Customers with open items, in order of their first occurrence in the invoice list. */
    public static List<String> customersWithOpenItems(List<Invoice> invoices) {
        List<String> customers = new ArrayList<>();
        for (Invoice inv : invoices) {
            if (!inv.isSettled() && !customers.contains(inv.customerId())) {
                customers.add(inv.customerId());
            }
        }
        return customers;
    }

    /** Open amount per customer in the same order. */
    public static Map<String, BigDecimal> openByCustomer(List<Invoice> invoices) {
        Map<String, BigDecimal> out = new LinkedHashMap<>();
        for (String customer : customersWithOpenItems(invoices)) {
            BigDecimal sum = Money.ZERO;
            for (Invoice inv : invoices) {
                if (inv.customerId().equals(customer) && !inv.isSettled()) {
                    sum = sum.add(inv.open());
                }
            }
            out.put(customer, sum);
        }
        return out;
    }

    public static String summary(Reconciler.Result result, List<Invoice> invoices, LocalDate today) {
        StringBuilder sb = new StringBuilder("Reconciliation of ").append(today).append("\n\n");
        sb.append(Reconciler.explain(result)).append('\n');
        sb.append("Open items by age:\n");
        aging(invoices, today).forEach((k, v) -> sb.append(String.format("  %-13s %14s%n", k, Money.format(v))));
        List<Dunning> dunning = dunningList(invoices, today);
        sb.append("\nDunning notices: ").append(dunning.size()).append('\n');
        for (Dunning d : dunning.subList(0, Math.min(15, dunning.size()))) {
            sb.append(String.format("  level %d  %-14s %-8s %4d days %12s%n", d.level(), d.invoice(), d.customer(), d.days(), Money.format(d.open())));
        }
        Map<String, BigDecimal> open = openByCustomer(invoices);
        sb.append("\nCustomers with open items: ").append(open.size()).append('\n');
        return sb.toString();
    }
}
