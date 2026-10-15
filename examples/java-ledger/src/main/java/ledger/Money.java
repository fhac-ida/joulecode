package ledger;

import java.math.BigDecimal;
import java.math.RoundingMode;
import java.util.Locale;

/** Amounts: always two decimal places, rounded half up (HALF_UP). */
public final class Money {

    public static final BigDecimal ZERO = new BigDecimal("0.00");
    public static final BigDecimal HUNDRED = new BigDecimal("100");

    private Money() {
    }

    public static BigDecimal of(BigDecimal value) {
        return value.setScale(2, RoundingMode.HALF_UP);
    }

    public static BigDecimal of(String value) {
        return of(new BigDecimal(value.trim().replace(',', '.')));
    }

    public static BigDecimal percent(BigDecimal base, BigDecimal percent) {
        return of(base.multiply(percent).divide(HUNDRED, 10, RoundingMode.HALF_UP));
    }

    /** German notation: 1.234,56 */
    public static String format(BigDecimal value) {
        return String.format(Locale.GERMANY, "%,.2f", value);
    }

    /** Parse from German notation (1.234,56 or -12,5). */
    public static BigDecimal parseGerman(String text) {
        String t = text.trim().replace(".", "").replace(',', '.');
        if (!t.matches("-?\\d+(\\.\\d{1,2})?")) {
            throw new IllegalArgumentException("not an amount: " + text);
        }
        return of(new BigDecimal(t));
    }
}
