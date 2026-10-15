package holdout;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertSame;

import java.math.BigDecimal;
import java.time.LocalDate;
import java.util.List;
import java.util.Map;
import ledger.Account;
import ledger.Iban;
import ledger.Invoice;
import ledger.Money;
import ledger.Reconciler;
import ledger.Report;
import ledger.Statement;
import ledger.Transaction;
import org.junit.jupiter.api.Test;

/** Holdout tests for java-ledger – joule:code does not see them. */
class LedgerHoldoutTest {

    static final LocalDate D = LocalDate.of(2026, 4, 10);
    static final String IBAN = Iban.withChecksum("DE", "500105170000000002");

    static Invoice inv(String number, String customer, String amount, int dueInDays) {
        return new Invoice(number, customer, D.minusDays(5), D.plusDays(dueInDays), new BigDecimal(amount), "EUR");
    }

    static Transaction tx(String id, String amount, String purpose, String customer) {
        return new Transaction(id, "K1", D, D, new BigDecimal(amount), "EUR", IBAN, "Customer", purpose, customer);
    }

    @Test
    void invoiceLookupIgnoresCaseAndFirstWins() {
        Invoice first = inv("re-2026-00001", "C1", "10.00", 5);
        Invoice second = inv("RE-2026-00001", "C2", "20.00", 5);
        Reconciler r = new Reconciler(List.of(first, second));
        assertSame(first, r.findInvoice("RE-2026-00001"));
        assertSame(first, r.findInvoice("Re-2026-00001"));
        assertNull(r.findInvoice("RE-2026-0001"));
    }

    @Test
    void invoiceAddedLaterIsFound() {
        Reconciler r = new Reconciler(List.of(inv("RE-2026-00001", "C1", "10.00", 5)));
        Invoice later = inv("RE-2026-00002", "C1", "30.00", 1);
        r.addInvoice(later);
        assertSame(later, r.findInvoice("re-2026-00002"));
        assertEquals(List.of(later, r.findInvoice("RE-2026-00001")), r.openInvoicesOf("C1"));
    }

    @Test
    void settledInvoicesAreNotOfferedAgain() {
        Reconciler r = new Reconciler(List.of(inv("RE-2026-00001", "C1", "50.00", 5), inv("RE-2026-00002", "C1", "50.00", 9)));
        Reconciler.Result res = r.reconcile(List.of(tx("T1", "50.00", "x", "C1"), tx("T2", "50.00", "y", "C1")));
        assertEquals(List.of("RE-2026-00001", "RE-2026-00002"), res.settled());
        assertEquals(List.of(), r.openInvoicesOf("C1"));
    }

    @Test
    void openInvoicesOrderedByDueThenNumber() {
        Reconciler r = new Reconciler(List.of(
                inv("RE-2026-00009", "C1", "1.00", 5),
                inv("RE-2026-00003", "C1", "1.00", 9),
                inv("RE-2026-00004", "C1", "1.00", 5),
                inv("RE-2026-00001", "C2", "1.00", 1)));
        assertEquals(List.of("RE-2026-00004", "RE-2026-00009", "RE-2026-00003"),
                r.openInvoicesOf("C1").stream().map(Invoice::number).toList());
    }

    @Test
    void referenceOrderAndDuplicatesInPurpose() {
        Reconciler r = new Reconciler(List.of(inv("RE-2026-00001", "C1", "30.00", 5), inv("RE-2026-00002", "C1", "30.00", 5)));
        Reconciler.Result res = r.reconcile(List.of(tx("T1", "40.00", "RE-2026-00002 re 2026 1 RE-2026-00002", null)));
        assertEquals(List.of("RE-2026-00002", "RE-2026-00001"), res.matches().stream().map(Reconciler.Match::invoiceNumber).toList());
        assertEquals(new BigDecimal("10.00"), res.matches().get(1).amount());
    }

    @Test
    void transactionsProcessedByBookingDateThenId() {
        Reconciler r = new Reconciler(List.of(inv("RE-2026-00001", "C1", "50.00", 5)));
        Transaction later = new Transaction("A", "K1", D.plusDays(1), D.plusDays(1), new BigDecimal("50.00"), "EUR", IBAN, "K", "x", "C1");
        Transaction earlier = new Transaction("B", "K1", D, D, new BigDecimal("50.00"), "EUR", IBAN, "K", "y", "C1");
        Reconciler.Result res = r.reconcile(List.of(later, earlier));
        assertEquals("B", res.matches().get(0).transactionId());
        assertEquals(List.of(later), res.unmatched());
    }

    @Test
    void customersWithOpenItemsKeepFirstOccurrence() {
        List<Invoice> invoices = List.of(inv("RE-2026-00001", "C9", "1.00", 1), inv("RE-2026-00002", "C1", "1.00", 1),
                inv("RE-2026-00003", "C9", "2.00", 1), inv("RE-2026-00004", "C5", "1.00", 1));
        invoices.get(3).apply(new BigDecimal("1.00"));
        assertEquals(List.of("C9", "C1"), Report.customersWithOpenItems(invoices));
        assertEquals(Map.of("C9", new BigDecimal("3.00"), "C1", new BigDecimal("1.00")), Report.openByCustomer(invoices));
        assertEquals(List.of("C9", "C1"), List.copyOf(Report.openByCustomer(invoices).keySet()));
    }

    @Test
    void statementTextIsStable() {
        Account a = new Account("K1", "DE89370400440532013000", "Company", "EUR", new BigDecimal("0"));
        Statement st = new Statement(a, Money.ZERO, List.of(tx("T1", "1234.50", "Payment   with  whitespace", null)), D, D);
        String expected = "Account statement Company – DE89 3704 0044 0532 0130 00\n"
                + String.format("%-10s %-12s %-24s %-40s %14s %14s%n", "Date", "Transaction", "Counterparty", "Purpose", "Amount", "Balance")
                + String.format("%-10s %-12s %-24s %-40s %14s %14s%n", "", "", "", "Opening balance", "", "0,00")
                + String.format("%-10s %-12s %-24s %-40s %14s %14s%n", "2026-04-10", "T1", "Customer", "Payment with whitespace", "1.234,50", "1.234,50")
                + String.format("%-10s %-12s %-24s %-40s %14s %14s%n", "", "", "", "Closing balance", "", "1.234,50");
        assertEquals(expected, st.render());
    }
}
