const express = require('express');
const path = require('path');

const app = express();
const PORT = process.env.PORT || 8080;

// Serve all static files from `public`.
app.use(express.static(path.join(__dirname, 'public')));

// Explicit root route keeps startup behavior obvious for beginners.
app.get('/', (_req, res) => {
  res.sendFile(path.join(__dirname, 'public', 'index.html'));
});

app.listen(PORT, '0.0.0.0', () => {
  console.log(`Static viewer server running on http://0.0.0.0:${PORT}`);
});
