package ledger;

import java.util.Locale;

/** IBAN check (modulo 97, ISO 13616) and formatting. */
public final class Iban {

    private Iban() {
    }

    public static String normalize(String iban) {
        return iban == null ? "" : iban.replace(" ", "").toUpperCase(Locale.ROOT);
    }

    public static boolean isValid(String raw) {
        String iban = normalize(raw);
        if (iban.length() < 15 || iban.length() > 34 || !iban.matches("[A-Z]{2}\\d{2}[A-Z0-9]+")) {
            return false;
        }
        String rearranged = iban.substring(4) + iban.substring(0, 4);
        int remainder = 0;
        for (int i = 0; i < rearranged.length(); i++) {
            char c = rearranged.charAt(i);
            int value = Character.isDigit(c) ? c - '0' : c - 'A' + 10;
            remainder = value >= 10 ? (remainder * 100 + value) % 97 : (remainder * 10 + value) % 97;
        }
        return remainder == 1;
    }

    /** In groups of four: DE89 3704 0044 0532 0130 00 */
    public static String format(String raw) {
        String iban = normalize(raw);
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < iban.length(); i += 4) {
            if (i > 0) {
                sb.append(' ');
            }
            sb.append(iban, i, Math.min(i + 4, iban.length()));
        }
        return sb.toString();
    }

    /** Compute the check digits (for test data): country code + "00" + BBAN → valid IBAN. */
    public static String withChecksum(String country, String bban) {
        String rearranged = bban + country + "00";
        int remainder = 0;
        for (int i = 0; i < rearranged.length(); i++) {
            char c = rearranged.charAt(i);
            int value = Character.isDigit(c) ? c - '0' : c - 'A' + 10;
            remainder = value >= 10 ? (remainder * 100 + value) % 97 : (remainder * 10 + value) % 97;
        }
        return country + String.format("%02d", 98 - remainder) + bban;
    }
}
