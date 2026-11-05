module.exports = {
  content: ['./src/gilgamosh/templates/**/*.html', './src/gilgamosh/static/*.js'],
  theme: {extend: {}},
  plugins: [require('daisyui')],
  daisyui: {themes: ['light']}
};
