module.exports = {
  content: ['./src/gilgamosh/templates/**/*.html', './src/gilgamosh/static/*.js'],
  theme: {extend: {}},
  plugins: [require('daisyui')],
  daisyui: {
    logs: false,
    themes: [{
      gilgamosh: {
        primary: '#0f766e',
        'primary-content': '#ffffff',
        secondary: '#334155',
        accent: '#0f766e',
        neutral: '#1e293b',
        'neutral-content': '#f8fafc',
        'base-100': '#ffffff',
        'base-200': '#f5f7f9',
        'base-300': '#e2e8f0',
        'base-content': '#1e293b',
        info: '#2a78d6',
        success: '#0f766e',
        warning: '#b45309',
        error: '#b91c1c',
        '--rounded-box': '0.875rem',
        '--rounded-btn': '0.5rem',
        '--rounded-badge': '9999px',
        '--tab-radius': '0.5rem'
      }
    }]
  }
};
