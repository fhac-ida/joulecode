package ledger;

import java.math.BigDecimal;
import java.time.LocalDate;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;

/**
 * Account statement: transactions of an account in the period with a running balance, as text and CSV.
 * Sorted by booking date, then ID; the purpose is shortened to 40 characters.
 */
public final class Statement {

    public record Line(LocalDate date, String id, String counterparty, String purpose, BigDecimal amount, BigDecimal balance) {
    }

    private final Account account;
    private final BigDecimal opening;
    private final List<Line> lines = new ArrayList<>();

    public Statement(Account account, BigDecimal opening, List<Transaction> transactions, LocalDate from, LocalDate to) {
        this.account = account;
        this.opening = Money.of(opening);
        List<Transaction> mine = new ArrayList<>();
        for (Transaction tx : transactions) {
            if (tx.accountId().equals(account.id()) && !tx.bookingDate().isBefore(from) && !tx.bookingDate().isAfter(to)) {
                mine.add(tx);
            }
        }
        mine.sort(Comparator.comparing(Transaction::bookingDate).thenComparing(Transaction::id));
        BigDecimal balance = this.opening;
        for (Transaction tx : mine) {
            balance = balance.add(tx.amount());
            lines.add(new Line(tx.bookingDate(), tx.id(), tx.counterpartyName(), shorten(tx.purpose(), 40), tx.amount(), balance));
        }
    }

    static String shorten(String text, int max) {
        String t = text.replaceAll("\\s+", " ").trim();
        return t.length() <= max ? t : t.substring(0, max - 1) + "…";
    }

    public List<Line> lines() {
        return lines;
    }

    public BigDecimal closing() {
        return lines.isEmpty() ? opening : lines.get(lines.size() - 1).balance();
    }

    public BigDecimal credits() {
        return lines.stream().map(Line::amount).filter(a -> a.signum() > 0).reduce(Money.ZERO, BigDecimal::add);
    }

    public BigDecimal debits() {
        return lines.stream().map(Line::amount).filter(a -> a.signum() < 0).reduce(Money.ZERO, BigDecimal::add);
    }

    /** Statement as text with fixed column widths. */
    public String render() {
        String out = "Account statement " + account.owner() + " – " + Iban.format(account.iban()) + "\n";
        out += String.format("%-10s %-12s %-24s %-40s %14s %14s%n", "Date", "Transaction", "Counterparty", "Purpose", "Amount", "Balance");
        out += String.format("%-10s %-12s %-24s %-40s %14s %14s%n", "", "", "", "Opening balance", "", Money.format(opening));
        for (Line l : lines) {
            out += String.format("%-10s %-12s %-24s %-40s %14s %14s%n", l.date(), l.id(),
                    shorten(l.counterparty() == null ? "" : l.counterparty(), 24), l.purpose(),
                    Money.format(l.amount()), Money.format(l.balance()));
        }
        out += String.format("%-10s %-12s %-24s %-40s %14s %14s%n", "", "", "", "Closing balance", "", Money.format(closing()));
        return out;
    }

    /** Statement as CSV (semicolon, German amounts). */
    public String csv() {
        StringBuilder sb = new StringBuilder("date;transaction;counterparty;purpose;amount;balance\n");
        for (Line l : lines) {
            sb.append(l.date()).append(';').append(l.id()).append(';')
                    .append(l.counterparty() == null ? "" : l.counterparty().replace(';', ',')).append(';')
                    .append(l.purpose().replace(';', ',')).append(';')
                    .append(Money.format(l.amount())).append(';').append(Money.format(l.balance())).append('\n');
        }
        return sb.toString();
    }
}
