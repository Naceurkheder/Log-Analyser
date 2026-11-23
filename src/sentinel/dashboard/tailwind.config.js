/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    './src/sentinel/dashboard/**/*.py',
    './dashboard_app.py',
  ],
  theme: {
    extend: {
      colors: {
        ink: '#10212B',
        paper: '#E9F0EE',
        steel: '#AFC3C4',
        cpu: '#E08A3C',
        gpu: '#2F80ED',
        alert: '#D64550',
        moss: '#2E6F5E',
      },
      fontFamily: {
        sans: ['"IBM Plex Sans"', 'ui-sans-serif', 'system-ui', 'sans-serif'],
        mono: ['"IBM Plex Mono"', 'ui-monospace', 'SFMono-Regular', 'monospace'],
      },
      letterSpacing: {
        signal: '0.14em',
      },
    },
  },
  plugins: [],
};
