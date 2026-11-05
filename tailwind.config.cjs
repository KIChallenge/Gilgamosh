const ramp = name => Object.fromEntries(
  [50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 950].map(step => [step, `rgb(var(--${name}-${step}) / <alpha-value>)`])
);

module.exports = {
  content: ['./src/gilgamosh/templates/**/*.html', './src/gilgamosh/static/*.js'],
  theme: {
    extend: {
      // Ramps resolve through CSS variables, so that both themes share every utility class.
      colors: {
        slate: ramp('slate'), teal: ramp('teal'), amber: ramp('amber'), red: ramp('red'), blue: ramp('blue'),
        surface: 'rgb(var(--surface) / <alpha-value>)',
        ink: '#0f172a'
      }
    }
  },
  plugins: [require('daisyui')],
  daisyui: {
    logs: false,
    themes: [
      {
        gilgamosh: {
          primary: '#0f766e', 'primary-content': '#ffffff', secondary: '#334155', accent: '#0f766e',
          neutral: '#1e293b', 'neutral-content': '#f8fafc',
          'base-100': '#ffffff', 'base-200': '#f5f7f9', 'base-300': '#e2e8f0', 'base-content': '#1e293b',
          info: '#2a78d6', success: '#0f766e', warning: '#b45309', error: '#b91c1c',
          '--rounded-box': '0.875rem', '--rounded-btn': '0.5rem', '--rounded-badge': '9999px', '--tab-radius': '0.5rem'
        },
        'gilgamosh-dark': {
          primary: '#0d7f76', 'primary-content': '#ffffff', secondary: '#c6d0de', accent: '#0d7f76',
          neutral: '#253247', 'neutral-content': '#eff3f8',
          'base-100': '#141c2b', 'base-200': '#1b2638', 'base-300': '#2f3e56', 'base-content': '#dde4ee',
          info: '#3987e5', success: '#2fb5a8', warning: '#f0b63d', error: '#ee8589',
          '--rounded-box': '0.875rem', '--rounded-btn': '0.5rem', '--rounded-badge': '9999px', '--tab-radius': '0.5rem'
        }
      }
    ]
  }
};
