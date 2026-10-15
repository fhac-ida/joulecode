package ledger;

import java.math.BigDecimal;
import java.math.RoundingMode;
import java.time.LocalDate;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.stream.Collectors;

/**
 * Reconciliation of incoming payments with open invoices.
 *
 * <p>Order per credit (transactions by booking date, then ID):
 * <ol>
 *   <li>invoice numbers in the purpose (“RE-2026-00042”, also “re 2026 42”): distribute the payment
 *       across these invoices in the order they are mentioned; the rest is an overpayment.</li>
 *   <li>otherwise open invoices of the customer (customer from the transaction or learnt via the IBAN):
 *       <ul>
 *         <li>amount matches exactly one invoice → the one with the earliest due date
 *             (on a tie the smallest number);</li>
 *         <li>amount = open amount minus 2% early payment discount (Skonto) and payment at most 14 days after
 *             the invoice date → the invoice counts as paid, the discount is reported;</li>
 *         <li>amount ≤ sum of all open invoices → partial payments, oldest due date first;</li>
 *       </ul></li>
 *   <li>otherwise: not assigned.</li>
 * </ol>
 * Invoice numbers are compared case-insensitively. If a number exists
 * twice, the invoice recorded first applies.
 */
public final class Reconciler {

    public enum Kind { REFERENCE, EXACT, SKONTO, PARTIAL }

    public record Match(String transactionId, String invoiceNumber, BigDecimal amount, Kind kind) {
    }

    public record Result(
            List<Match> matches,
            Map<String, BigDecimal> overpayments,
            List<Transaction> unmatched,
            List<String> settled,
            Map<String, BigDecimal> skonto,
            List<String> warnings) {
    }

    static final String REF_REGEX = "(?i)\\bRE[-\\s]?(\\d{4})[-\\s]?(\\d{1,6})\\b";
    static final BigDecimal SKONTO_PERCENT = new BigDecimal("2");
    static final int SKONTO_DAYS = 14;

    private final List<Invoice> invoices = new ArrayList<>();
    private final Map<String, String> customerByIban = new HashMap<>();

    public Reconciler(List<Invoice> invoices) {
        this.invoices.addAll(invoices);
    }

    public void addInvoice(Invoice invoice) {
        invoices.add(invoice);
    }

    public List<Invoice> invoices() {
        return invoices;
    }

    /** Known mapping IBAN → customer (e.g. from earlier reconciliations). */
    public void learn(String iban, String customerId) {
        customerByIban.putIfAbsent(Iban.normalize(iban), customerId);
    }

    /** Invoice for the number (case-insensitive), with duplicates the one recorded first. */
    public Invoice findInvoice(String number) {
        for (Invoice inv : invoices) {
            if (inv.number().equalsIgnoreCase(number)) {
                return inv;
            }
        }
        return null;
    }

    /** Open invoices of a customer, by due date, then number. */
    public List<Invoice> openInvoicesOf(String customerId) {
        return invoices.stream()
                .filter(inv -> inv.customerId().equals(customerId) && !inv.isSettled())
                .sorted(Comparator.comparing(Invoice::due).thenComparing(Invoice::number))
                .collect(Collectors.toList());
    }

    /** Invoice numbers from a purpose, normalised, in the order they are mentioned, without duplicates. */
    public static List<String> extractReferences(String purpose) {
        Pattern pattern = Pattern.compile(REF_REGEX);
        Matcher m = pattern.matcher(purpose);
        List<String> refs = new ArrayList<>();
        while (m.find()) {
            String number = String.format("RE-%s-%05d", m.group(1), Integer.parseInt(m.group(2)));
            if (!refs.contains(number)) {
                refs.add(number);
            }
        }
        return refs;
    }

    public String customerOf(Transaction tx) {
        if (tx.customerId() != null && !tx.customerId().isBlank()) {
            return tx.customerId();
        }
        return customerByIban.get(Iban.normalize(tx.counterpartyIban()));
    }

    public Result reconcile(List<Transaction> transactions) {
        List<Match> matches = new ArrayList<>();
        Map<String, BigDecimal> overpayments = new LinkedHashMap<>();
        List<Transaction> unmatched = new ArrayList<>();
        List<String> settled = new ArrayList<>();
        Map<String, BigDecimal> skonto = new LinkedHashMap<>();
        List<String> warnings = new ArrayList<>();

        List<Transaction> ordered = new ArrayList<>(transactions);
        ordered.sort(Comparator.comparing(Transaction::bookingDate).thenComparing(Transaction::id));
        Set<String> seen = new HashSet<>();
        for (Transaction tx : ordered) {
            if (!seen.add(tx.id())) {
                warnings.add("transaction " + tx.id() + " delivered twice – ignored");
                continue;
            }
            if (!tx.isCredit()) {
                continue;
            }
            BigDecimal rest = byReference(tx, matches, settled, warnings);
            if (rest == null) {
                rest = byCustomer(tx, matches, settled, skonto);
            }
            if (rest == null) {
                unmatched.add(tx);
            } else if (rest.signum() > 0) {
                overpayments.merge(tx.id(), rest, BigDecimal::add);
            }
        }
        return new Result(matches, overpayments, unmatched, settled, skonto, warnings);
    }

    /** Assignment via invoice numbers; null if no matching number was found. */
    private BigDecimal byReference(Transaction tx, List<Match> matches, List<String> settled, List<String> warnings) {
        List<Invoice> targets = new ArrayList<>();
        for (String ref : extractReferences(tx.purpose())) {
            Invoice inv = findInvoice(ref);
            if (inv == null) {
                warnings.add("transaction " + tx.id() + ": invoice " + ref + " unknown");
            } else if (!inv.currency().equals(tx.currency())) {
                warnings.add("transaction " + tx.id() + ": currency does not match " + inv.number());
            } else {
                targets.add(inv);
            }
        }
        if (targets.isEmpty()) {
            return null;
        }
        BigDecimal rest = tx.amount();
        for (Invoice inv : targets) {
            if (rest.signum() <= 0) {
                break;
            }
            BigDecimal before = inv.open();
            if (before.signum() <= 0) {
                continue;
            }
            BigDecimal after = inv.apply(rest);
            BigDecimal used = rest.subtract(after);
            rest = after;
            matches.add(new Match(tx.id(), inv.number(), used, Kind.REFERENCE));
            if (inv.isSettled()) {
                settled.add(inv.number());
            }
        }
        return rest;
    }

    /** Assignment via the customer; null if nothing matches. */
    private BigDecimal byCustomer(Transaction tx, List<Match> matches, List<String> settled, Map<String, BigDecimal> skonto) {
        String customer = customerOf(tx);
        if (customer == null) {
            return null;
        }
        List<Invoice> open = openInvoicesOf(customer);
        if (open.isEmpty()) {
            return null;
        }
        BigDecimal amount = tx.amount();
        for (Invoice inv : open) {
            if (inv.open().compareTo(amount) == 0 && inv.currency().equals(tx.currency())) {
                inv.apply(amount);
                matches.add(new Match(tx.id(), inv.number(), amount, Kind.EXACT));
                settled.add(inv.number());
                return Money.ZERO;
            }
        }
        for (Invoice inv : open) {
            BigDecimal discount = Money.percent(inv.open(), SKONTO_PERCENT);
            boolean inTime = !tx.valueDate().isAfter(inv.issued().plusDays(SKONTO_DAYS));
            if (inTime && inv.open().subtract(discount).compareTo(amount) == 0) {
                inv.apply(amount.add(discount));
                matches.add(new Match(tx.id(), inv.number(), amount, Kind.SKONTO));
                skonto.put(inv.number(), discount);
                settled.add(inv.number());
                return Money.ZERO;
            }
        }
        BigDecimal total = open.stream().map(Invoice::open).reduce(Money.ZERO, BigDecimal::add);
        if (amount.compareTo(total) > 0) {
            return null;
        }
        BigDecimal rest = amount;
        for (Invoice inv : open) {
            if (rest.signum() == 0) {
                break;
            }
            BigDecimal after = inv.apply(rest);
            matches.add(new Match(tx.id(), inv.number(), rest.subtract(after), Kind.PARTIAL));
            rest = after;
            if (inv.isSettled()) {
                settled.add(inv.number());
            }
        }
        return rest;
    }

    // -- Analyses of the reconciliation ------------------------------------------------------------

    /** How much was paid per invoice and by which route? */
    public static Map<Kind, BigDecimal> totalsByKind(List<Match> matches) {
        Map<Kind, BigDecimal> out = new LinkedHashMap<>();
        for (Kind k : Kind.values()) {
            out.put(k, Money.ZERO);
        }
        for (Match m : matches) {
            out.merge(m.kind(), m.amount(), BigDecimal::add);
        }
        return out;
    }

    /**
     * Suggestions for unassigned transactions: invoices with exactly this open amount
     * (any customer), at most three, oldest due date first.
     */
    public Map<String, List<String>> suggestions(List<Transaction> unmatched) {
        Map<String, List<String>> out = new LinkedHashMap<>();
        for (Transaction tx : unmatched) {
            List<String> hits = invoices.stream()
                    .filter(inv -> !inv.isSettled() && inv.open().compareTo(tx.amount()) == 0)
                    .sorted(Comparator.comparing(Invoice::due).thenComparing(Invoice::number))
                    .limit(3)
                    .map(Invoice::number)
                    .collect(Collectors.toList());
            if (!hits.isEmpty()) {
                out.put(tx.id(), hits);
            }
        }
        return out;
    }

    /** Learn IBANs: every payment assigned unambiguously via an invoice number links IBAN and customer. */
    public int learnFrom(List<Transaction> transactions, List<Match> matches) {
        Map<String, Transaction> byId = new HashMap<>();
        for (Transaction tx : transactions) {
            byId.putIfAbsent(tx.id(), tx);
        }
        int learned = 0;
        for (Match m : matches) {
            if (m.kind() != Kind.REFERENCE) {
                continue;
            }
            Transaction tx = byId.get(m.transactionId());
            Invoice inv = findInvoice(m.invoiceNumber());
            if (tx == null || inv == null || tx.counterpartyIban() == null) {
                continue;
            }
            String iban = Iban.normalize(tx.counterpartyIban());
            if (!customerByIban.containsKey(iban)) {
                customerByIban.put(iban, inv.customerId());
                learned++;
            }
        }
        return learned;
    }

    /** Transactions that are probably duplicates (same IBAN, amount, day and purpose, different ID). */
    public static List<List<String>> probableDuplicates(List<Transaction> transactions) {
        Map<String, List<String>> groups = new LinkedHashMap<>();
        for (Transaction tx : transactions) {
            String key = String.join("|", Iban.normalize(tx.counterpartyIban()), tx.amount().toPlainString(),
                    tx.bookingDate().toString(), tx.purpose().trim().toLowerCase(Locale.ROOT));
            groups.computeIfAbsent(key, k -> new ArrayList<>()).add(tx.id());
        }
        return groups.values().stream().filter(ids -> ids.size() > 1).collect(Collectors.toList());
    }

    /** Average payment duration per customer in days (invoice date → last payment). */
    public Map<String, Double> averageDaysToPay(List<Transaction> transactions, List<Match> matches) {
        Map<String, LocalDate> dateByTx = new HashMap<>();
        for (Transaction tx : transactions) {
            dateByTx.putIfAbsent(tx.id(), tx.valueDate());
        }
        Map<String, LocalDate> lastPayment = new HashMap<>();
        for (Match m : matches) {
            LocalDate d = dateByTx.get(m.transactionId());
            if (d != null) {
                lastPayment.merge(m.invoiceNumber(), d, (a, b) -> a.isAfter(b) ? a : b);
            }
        }
        Map<String, List<Long>> days = new HashMap<>();
        for (Invoice inv : invoices) {
            LocalDate paid = lastPayment.get(inv.number());
            if (paid != null && inv.isSettled()) {
                days.computeIfAbsent(inv.customerId(), k -> new ArrayList<>()).add(paid.toEpochDay() - inv.issued().toEpochDay());
            }
        }
        Map<String, Double> out = new java.util.TreeMap<>();
        days.forEach((customer, list) -> out.put(customer,
                BigDecimal.valueOf(list.stream().mapToLong(Long::longValue).average().orElse(0))
                        .setScale(1, RoundingMode.HALF_UP).doubleValue()));
        return out;
    }

    /**
     * Return debits: debits whose purpose contains “RUECKLASTSCHRIFT” (German for return debit) and an
     * invoice number reopen the invoice by the returned amount
     * (at most up to the invoice amount). Returns the affected invoice numbers.
     */
    public List<String> applyReturns(List<Transaction> transactions, List<String> warnings) {
        List<String> reopened = new ArrayList<>();
        for (Transaction tx : transactions) {
            if (tx.isCredit() || !tx.purpose().toUpperCase(Locale.ROOT).contains("RUECKLASTSCHRIFT")) {
                continue;
            }
            List<String> refs = extractReferences(tx.purpose());
            if (refs.isEmpty()) {
                warnings.add("return debit " + tx.id() + " without invoice number");
                continue;
            }
            Invoice inv = findInvoice(refs.get(0));
            if (inv == null) {
                warnings.add("return debit " + tx.id() + ": invoice " + refs.get(0) + " unknown");
                continue;
            }
            BigDecimal back = tx.amount().negate().min(inv.paid());
            if (back.signum() > 0) {
                inv.apply(back.negate());
                if (!reopened.contains(inv.number())) {
                    reopened.add(inv.number());
                }
            }
        }
        return reopened;
    }

    /** Reconciliation in words (for the accounting log). */
    public static String explain(Result r) {
        StringBuilder sb = new StringBuilder();
        Map<Kind, BigDecimal> totals = totalsByKind(r.matches());
        sb.append("Assignments: ").append(r.matches().size()).append('\n');
        totals.forEach((k, v) -> sb.append(String.format("  %-9s %14s%n", k, Money.format(v))));
        sb.append("Settled invoices: ").append(r.settled().size()).append('\n');
        if (!r.skonto().isEmpty()) {
            BigDecimal sum = r.skonto().values().stream().reduce(Money.ZERO, BigDecimal::add);
            sb.append("Early payment discount granted: ").append(Money.format(sum)).append(" (").append(r.skonto().size()).append(" invoices)\n");
        }
        if (!r.overpayments().isEmpty()) {
            BigDecimal sum = r.overpayments().values().stream().reduce(Money.ZERO, BigDecimal::add);
            sb.append("Overpayments: ").append(Money.format(sum)).append('\n');
        }
        sb.append("Not assigned: ").append(r.unmatched().size()).append('\n');
        for (String w : r.warnings()) {
            sb.append("  ! ").append(w).append('\n');
        }
        return sb.toString();
    }

    /** Plausibility check of the invoice data. */
    public List<String> validate() {
        List<String> problems = new ArrayList<>();
        Set<String> numbers = new HashSet<>();
        for (Invoice inv : invoices) {
            if (!numbers.add(inv.number().toUpperCase(Locale.ROOT))) {
                problems.add("duplicate invoice number: " + inv.number());
            }
            if (inv.amount().signum() <= 0) {
                problems.add(inv.number() + ": amount not positive");
            }
            if (!inv.number().matches("(?i)RE-\\d{4}-\\d{5}")) {
                problems.add(inv.number() + ": number does not match the pattern RE-YYYY-NNNNN");
            }
        }
        return problems;
    }
}
