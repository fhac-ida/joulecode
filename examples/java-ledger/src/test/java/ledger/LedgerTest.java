package ledger;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.math.BigDecimal;
import java.time.LocalDate;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;

class LedgerTest {

    static final LocalDate D = LocalDate.of(2026, 4, 10);
    static final String IBAN = Iban.withChecksum("DE", "500105170000000001");

    static Invoice inv(String number, String customer, String amount, int dueInDays) {
        return new Invoice(number, customer, D.minusDays(5), D.plusDays(dueInDays), new BigDecimal(amount), "EUR");
    }

    static Transaction tx(String id, String amount, String purpose, String customer) {
        return new Transaction(id, "K1", D, D, new BigDecimal(amount), "EUR", IBAN, "Customer", purpose, customer);
    }

    @Test
    void ibanCheck() {
        assertTrue(Iban.isValid("DE89 3704 0044 0532 0130 00"));
        assertFalse(Iban.isValid("DE88 3704 0044 0532 0130 00"));
        assertEquals("DE89 3704 0044 0532 0130 00", Iban.format("de89370400440532013000"));
    }

    @Test
    void moneyParsingAndFormat() {
        assertEquals(new BigDecimal("1234.50"), Money.parseGerman("1.234,5"));
        assertEquals("1.234,50", Money.format(new BigDecimal("1234.5")));
        assertThrows(IllegalArgumentException.class, () -> Money.parseGerman("12x"));
    }

    @Test
    void extractReferencesNormalizes() {
        assertEquals(List.of("RE-2026-00042", "RE-2026-00007"), Reconciler.extractReferences("Payment re 2026 42 and RE-2026-00007, RE2026-42"));
    }

    @Test
    void paymentByReference() {
        Reconciler r = new Reconciler(List.of(inv("RE-2026-00001", "C1", "100.00", 20)));
        Reconciler.Result res = r.reconcile(List.of(tx("T1", "120.00", "RE-2026-00001", null)));
        assertEquals(1, res.matches().size());
        assertEquals(Reconciler.Kind.REFERENCE, res.matches().get(0).kind());
        assertEquals(List.of("RE-2026-00001"), res.settled());
        assertEquals(Map.of("T1", new BigDecimal("20.00")), res.overpayments());
    }

    @Test
    void exactAmountPicksEarliestDue() {
        Reconciler r = new Reconciler(List.of(inv("RE-2026-00002", "C1", "50.00", 30), inv("RE-2026-00001", "C1", "50.00", 10)));
        Reconciler.Result res = r.reconcile(List.of(tx("T1", "50.00", "Thanks", "C1")));
        assertEquals("RE-2026-00001", res.matches().get(0).invoiceNumber());
        assertEquals(Reconciler.Kind.EXACT, res.matches().get(0).kind());
    }

    @Test
    void skontoWithinFourteenDays() {
        Reconciler r = new Reconciler(List.of(inv("RE-2026-00003", "C1", "200.00", 30)));
        Reconciler.Result res = r.reconcile(List.of(tx("T1", "196.00", "Invoice April", "C1")));
        assertEquals(Reconciler.Kind.SKONTO, res.matches().get(0).kind());
        assertEquals(Map.of("RE-2026-00003", new BigDecimal("4.00")), res.skonto());
    }

    @Test
    void partialPaymentsOldestFirst() {
        Reconciler r = new Reconciler(List.of(inv("RE-2026-00005", "C1", "100.00", 20), inv("RE-2026-00004", "C1", "80.00", 5)));
        Reconciler.Result res = r.reconcile(List.of(tx("T1", "120.00", "Instalment", "C1")));
        assertEquals(2, res.matches().size());
        assertEquals("RE-2026-00004", res.matches().get(0).invoiceNumber());
        assertEquals(new BigDecimal("40.00"), res.matches().get(1).amount());
    }

    @Test
    void unknownCustomerIsUnmatched() {
        Reconciler r = new Reconciler(List.of(inv("RE-2026-00001", "C1", "100.00", 20)));
        Reconciler.Result res = r.reconcile(List.of(tx("T1", "100.00", "?", null)));
        assertEquals(1, res.unmatched().size());
        r.learn(IBAN, "C1");
        assertEquals(0, r.reconcile(List.of(tx("T2", "100.00", "?", null))).unmatched().size());
    }

    @Test
    void dunningLevels() {
        assertEquals(0, Report.dunningLevel(13));
        assertEquals(1, Report.dunningLevel(14));
        assertEquals(3, Report.dunningLevel(60));
    }

    @Test
    void statementRunningBalance() {
        Account a = new Account("K1", "DE89370400440532013000", "Company", "EUR", new BigDecimal("1000"));
        Statement st = new Statement(a, new BigDecimal("10"), List.of(tx("T2", "-5.00", "b", null), tx("T1", "20.00", "a", null)), D, D);
        assertEquals(new BigDecimal("25.00"), st.closing());
        assertEquals(List.of("T1", "T2"), st.lines().stream().map(Statement.Line::id).toList());
        assertTrue(st.render().contains("Closing balance"));
    }

    @Test
    void interestOnOverdraft() {
        Account a = new Account("K1", "DE89370400440532013000", "Company", "EUR", new BigDecimal("1000"));
        BigDecimal i = Interest.debitInterest(a, List.of(new BigDecimal("-3650.00")), Interest.DEFAULT);
        // 1000 € × 9.5% / 365 + 2650 € × 14.5% / 365 = 0.2603 + 1.0527
        assertEquals(new BigDecimal("1.31"), i);
    }
}
