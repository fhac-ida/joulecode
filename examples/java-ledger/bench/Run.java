import java.time.LocalDate;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Random;

import ledger.Account;
import ledger.Batch;
import ledger.Iban;

/**
 * Benchmark: fixed, deterministic amount of work (one month-end closing).
 *
 * BENCH_ROUNDS scales the run time (on the measurement machine, choose it so that a run takes >= 20 s).
 * BENCH_INVOICES / BENCH_TRANSACTIONS change the amount of data per round.
 */
public class Run {

    static int env(String name, int def) {
        String v = System.getenv(name);
        return v == null || v.isBlank() ? def : Integer.parseInt(v);
    }

    static String money(Random rng, int maxEuro) {
        int cents = 1000 + rng.nextInt(maxEuro * 100);
        return String.format("%d,%02d", cents / 100, cents % 100);
    }

    public static void main(String[] args) {
        int rounds = env("BENCH_ROUNDS", 3);
        int invoiceCount = env("BENCH_INVOICES", 6000);
        int txCount = env("BENCH_TRANSACTIONS", 5000);
        Random rng = new Random(1234);
        LocalDate from = LocalDate.of(2026, 4, 1);
        LocalDate to = LocalDate.of(2026, 4, 30);

        List<Account> accounts = new ArrayList<>();
        for (int a = 0; a < 4; a++) {
            accounts.add(new Account("K" + a, Iban.withChecksum("DE", String.format("37040044%010d", 532013000L + a)),
                    "Company " + a, "EUR", new java.math.BigDecimal(5000 * (a + 1))));
        }
        int customers = 900;
        List<String> invoiceLines = new ArrayList<>();
        List<String[]> invoices = new ArrayList<>();
        for (int i = 0; i < invoiceCount; i++) {
            String number = String.format("RE-2026-%05d", i + 1);
            String customer = "C" + rng.nextInt(customers);
            LocalDate issued = from.minusDays(60).plusDays(rng.nextInt(85));
            String amount = money(rng, 3000);
            invoiceLines.add(String.join(";", number, customer, issued.toString(), issued.plusDays(30).toString(), amount, "EUR"));
            invoices.add(new String[] {number, customer, amount, issued.toString()});
        }
        Map<String, String> knownIbans = new HashMap<>();
        List<String> txLines = new ArrayList<>();
        for (int t = 0; t < txCount; t++) {
            String[] inv = invoices.get(rng.nextInt(invoices.size()));
            String customer = inv[1];
            String iban = Iban.withChecksum("DE", String.format("50010517%010d", Integer.parseInt(customer.substring(1))));
            if (rng.nextInt(3) == 0) {
                knownIbans.put(iban, customer);
            }
            LocalDate day = from.plusDays(rng.nextInt(30));
            int kind = rng.nextInt(10);
            String purpose;
            String amount = inv[2];
            String customerField = "";
            if (kind < 5) {
                purpose = "Payment " + (kind == 0 ? inv[0].toLowerCase().replace("-", " ") : inv[0]) + " thanks";
            } else if (kind < 8) {
                purpose = "Customer no " + customer + " collective payment";
                customerField = kind == 5 ? customer : "";
            } else if (kind == 8) {
                purpose = "Salary April";
                amount = "-" + money(rng, 4000);
            } else {
                purpose = "RUECKLASTSCHRIFT " + inv[0] + " account closed";
                amount = "-" + inv[2];
            }
            txLines.add(String.join(";", String.format("T%06d", t), "K" + rng.nextInt(4), day.toString(), day.toString(),
                    amount, "EUR", iban, "Customer " + customer, purpose, customerField));
        }

        long start = System.nanoTime();
        long checksum = 0;
        for (int r = 0; r < rounds; r++) {
            Batch.Output out = Batch.run(accounts, txLines, invoiceLines, knownIbans, from, to);
            checksum += out.result().matches().size() * 7L + out.result().unmatched().size() * 3L + out.result().settled().size();
            checksum += out.reopened().size() + out.report().length() + out.errors().size();
            for (String s : out.statements().values()) {
                checksum += s.length();
            }
            checksum += out.interest().values().stream().mapToLong(b -> b.movePointRight(2).longValue()).sum();
            checksum += out.forecast().values().stream().mapToLong(b -> b.movePointRight(2).longValue()).sum() % 1000003;
        }
        System.out.printf("rounds=%d checksum=%d seconds=%.2f%n", rounds, checksum, (System.nanoTime() - start) / 1e9);
    }
}
