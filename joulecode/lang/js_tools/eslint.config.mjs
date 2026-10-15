// ESLint configuration for joule:code: only findings for the optimiser (like Ruff for Python),
// no style rules. Dead code: no-unused-vars.
import unicorn from 'eslint-plugin-unicorn';
import tseslint from 'typescript-eslint';
import globals from 'globals';

const rules = {
  'no-unused-vars': 'warn',
  'no-await-in-loop': 'warn',
  'no-loop-func': 'warn',
  'no-useless-concat': 'warn',
  'no-useless-call': 'warn',
  'unicorn/prefer-set-has': 'warn',
  'unicorn/prefer-array-find': 'warn',
  'unicorn/prefer-array-some': 'warn',
  'unicorn/prefer-includes': 'warn',
  'unicorn/prefer-array-flat-map': 'warn',
  'unicorn/prefer-array-index-of': 'warn',
  'unicorn/prefer-single-call': 'warn',
  'unicorn/prefer-string-replace-all': 'warn',
  'unicorn/prefer-math-min-max': 'warn',
  'unicorn/no-useless-spread': 'warn',
  'unicorn/no-new-array': 'warn',
};

export default [
  {
    files: ['**/*.{js,mjs,cjs,jsx}'],
    plugins: { unicorn },
    languageOptions: { ecmaVersion: 'latest', sourceType: 'module', globals: { ...globals.node, ...globals.browser } },
    rules,
  },
  {
    files: ['**/*.{ts,mts,cts,tsx}'],
    plugins: { unicorn, '@typescript-eslint': tseslint.plugin },
    languageOptions: { parser: tseslint.parser, ecmaVersion: 'latest', sourceType: 'module', globals: { ...globals.node, ...globals.browser } },
    rules: { ...rules, 'no-unused-vars': 'off', '@typescript-eslint/no-unused-vars': 'warn' },
  },
];
