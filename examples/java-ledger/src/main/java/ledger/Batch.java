package ledger;

import java.math.BigDecimal;
import java.time.LocalDate;
import java.time.YearMonth;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** Nightly run: read, return debits, reconciliation, learning IBANs, statements, fees, interest, report. */
public final class Batch {

    public record Output(Reconciler.Result result, List<String> reopened, Map<String, String> statements,
            Map<String, Map<YearMonth, BigDecimal>> fees, Map<String, BigDecimal> interest, String report,
            Map<LocalDate, BigDecimal> forecast, List<String> errors) {
    }

    private Batch() {
    }

    public static Output run(List<Account> accounts, List<String> transactionLines, List<String> invoiceLines,
            Map<String, String> knownIbans, LocalDate from, LocalDate to) {
        Parser.Parsed<Transaction> txs = Parser.transactions(transactionLines);
        Parser.Parsed<Invoice> invs = Parser.invoices(invoiceLines);
        List<String> errors = new ArrayList<>();
        errors.addAll(txs.errors());
        errors.addAll(invs.errors());

        Reconciler reconciler = new Reconciler(invs.items());
        knownIbans.forEach(reconciler::learn);
        List<String> warnings = new ArrayList<>();
        Reconciler.Result result = reconciler.reconcile(txs.items());
        List<String> reopened = reconciler.applyReturns(txs.items(), warnings);
        result.warnings().addAll(warnings);
        reconciler.learnFrom(txs.items(), result.matches());

        Map<String, String> statements = new LinkedHashMap<>();
        Map<String, BigDecimal> interest = new LinkedHashMap<>();
        for (Account a : accounts) {
            Statement st = new Statement(a, Money.ZERO, txs.items(), from, to);
            statements.put(a.id(), st.render());
            List<Transaction> mine = txs.items().stream().filter(t -> t.accountId().equals(a.id())).toList();
            interest.put(a.id(), Interest.debitInterest(a, Interest.dailyBalances(Money.ZERO, mine, from, to), Interest.DEFAULT));
        }
        Map<String, Map<YearMonth, BigDecimal>> fees = Fees.monthly(txs.items(), Fees.BUSINESS);
        String report = Report.summary(result, reconciler.invoices(), to);
        Map<LocalDate, BigDecimal> forecast = Forecast.weekly(reconciler.invoices(),
                reconciler.averageDaysToPay(txs.items(), result.matches()), to, 8);
        return new Output(result, reopened, statements, fees, interest, report, forecast, errors);
    }
}
