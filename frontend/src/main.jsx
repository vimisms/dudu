import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App.jsx";
import Results from "./Results.jsx";
import "./App.css";

// The results window loads the same bundle at #/results; everything else is
// the main avatar window.
const isResults = window.location.hash.replace(/^#\/?/, "").startsWith("results");

ReactDOM.createRoot(document.getElementById("root")).render(
  <React.StrictMode>{isResults ? <Results /> : <App />}</React.StrictMode>
);
