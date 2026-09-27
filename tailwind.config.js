/** @type {import('tailwindcss').Config} */
module.exports = {
  darkMode: 'class',
  content: [
    'src/taskmanager/web/static/index.html',
    'src/taskmanager/web/static/js/*.js',
    'src/taskmanager/web/ui.py',
    'src/taskmanager/web/static_export.py',
  ],
  theme: {
    extend: {
      fontFamily: {
        mono: ['"JetBrains Mono"', 'monospace'],
      },
      colors: {
        brand: {
          50: '#f0fdf4',
          500: '#22c55e',
          600: '#16a34a',
          900: '#14532d',
        },
      },
    },
  },
}
