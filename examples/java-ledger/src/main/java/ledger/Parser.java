package ledger;

import java.math.BigDecimal;
import java.time.LocalDate;
import java.time.format.DateTimeParseException;
import java.util.ArrayList;
import java.util.List;

/**
 * Reading account transactions and invoices from semicolon CSV (export of the bank or the invoicing system).
 *
 * <pre>
 * Transactions: id;account;booking_date;value_date;amount;currency;counter_iban;counter_name;purpose[;customer]
 * Invoices:     number;customer;date;due;amount;currency
 * </pre>
 * Amounts in German notation (1.234,56), dates ISO (2026-05-04).
 */
public final class Parser {

    public record Parsed<T>(List<T> items, List<String> errors) {
    }

    private Parser() {
    }

    static List<String> split(String line) {
        List<String> out = new ArrayList<>();
        StringBuilder cur = new StringBuilder();
        boolean quoted = false;
        for (int i = 0; i < line.length(); i++) {
            char c = line.charAt(i);
            if (quoted) {
                if (c == '"' && i + 1 < line.length() && line.charAt(i + 1) == '"') {
                    cur.append('"');
                    i++;
                } else if (c == '"') {
                    quoted = false;
                } else {
                    cur.append(c);
                }
            } else if (c == '"') {
                quoted = true;
            } else if (c == ';') {
                out.add(cur.toString());
                cur.setLength(0);
            } else {
                cur.append(c);
            }
        }
        out.add(cur.toString());
        return out;
    }

    public static Parsed<Transaction> transactions(List<String> lines) {
        List<Transaction> items = new ArrayList<>();
        List<String> errors = new ArrayList<>();
        int n = 0;
        for (String line : lines) {
            n++;
            if (line.isBlank() || line.startsWith("#")) {
                continue;
            }
            List<String> f = split(line);
            if (f.size() != 9 && f.size() != 10) {
                errors.add("line " + n + ": expected 9 or 10 fields, found " + f.size());
                continue;
            }
            try {
                items.add(new Transaction(f.get(0).trim(), f.get(1).trim(), LocalDate.parse(f.get(2).trim()),
                        LocalDate.parse(f.get(3).trim()), Money.parseGerman(f.get(4)), f.get(5).trim().toUpperCase(),
                        Iban.normalize(f.get(6)), f.get(7).trim(), f.get(8).trim(), f.size() == 10 ? f.get(9).trim() : null));
            } catch (DateTimeParseException | IllegalArgumentException e) {
                errors.add("line " + n + ": " + e.getMessage());
            }
        }
        return new Parsed<>(items, errors);
    }

    public static Parsed<Invoice> invoices(List<String> lines) {
        List<Invoice> items = new ArrayList<>();
        List<String> errors = new ArrayList<>();
        int n = 0;
        for (String line : lines) {
            n++;
            if (line.isBlank() || line.startsWith("#")) {
                continue;
            }
            List<String> f = split(line);
            if (f.size() != 6) {
                errors.add("line " + n + ": expected 6 fields, found " + f.size());
                continue;
            }
            try {
                BigDecimal amount = Money.parseGerman(f.get(4));
                items.add(new Invoice(f.get(0).trim(), f.get(1).trim(), LocalDate.parse(f.get(2).trim()),
                        LocalDate.parse(f.get(3).trim()), amount, f.get(5).trim().toUpperCase()));
            } catch (DateTimeParseException | IllegalArgumentException e) {
                errors.add("line " + n + ": " + e.getMessage());
            }
        }
        return new Parsed<>(items, errors);
    }
}
