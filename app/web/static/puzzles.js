/* Makes the rendered puzzle diagrams playable.
 *
 * The server already drew every board, in document order, all of them visible.
 * This script does three things to that: collapses the list to one puzzle at a
 * time, reveals the controls, and turns clicks on squares into a move. With
 * scripting off none of it happens and the section is still a set of diagrams
 * with links to the games -- which is why nothing here builds a board.
 *
 * There is no chess engine on this page and there does not need to be. Ingest
 * enumerated the legal moves once, with a real rules implementation, while the
 * position was in hand; all that is left at this end is a lookup.
 */
(function () {
  "use strict";

  var list = document.getElementById("puzzles");
  var source = document.getElementById("puzzle-data");
  if (!list || !source) return;

  var data = JSON.parse(source.textContent);
  var items = Array.prototype.slice.call(list.querySelectorAll(".puzzle"));
  if (items.length !== data.length) return;

  var current = 0;

  // The list stops being a list here, and says so. Stacked, each puzzle needs a
  // rule above it to separate it from the one before; one at a time, that rule
  // is a stray line and a gap above a board with nothing over it.
  list.classList.add("puzzle-list--one");

  items.forEach(function (item, index) {
    setup(item, data[index], index);
    if (index > 0) item.hidden = true;
  });

  function show(index) {
    if (index < 0 || index >= items.length) return;
    items[current].hidden = true;
    current = index;
    items[current].hidden = false;
    // Only ever scrolls when the reader asked for the next puzzle, never on
    // load: the report is read top to bottom and this section is near the end.
    items[current].scrollIntoView({ block: "center", behavior: "smooth" });
  }

  function setup(item, puzzle, index) {
    var board = item.querySelector(".board");
    var verdict = item.querySelector("[data-verdict]");
    var outcome = item.querySelector("[data-outcome]");
    var actions = item.querySelector("[data-actions]");
    var reveal = item.querySelector("[data-reveal]");
    var next = item.querySelector("[data-next]");
    var squares = {};
    var selected = null;
    var done = false;

    Array.prototype.forEach.call(board.querySelectorAll(".sq"), function (square) {
      squares[square.dataset.square] = square;
    });

    actions.hidden = false;

    board.addEventListener("click", function (event) {
      if (done) return;
      var square = event.target.closest(".sq");
      if (!square) return;
      var name = square.dataset.square;

      if (selected && (puzzle.legal_moves[selected] || []).indexOf(name) !== -1) {
        attempt(selected, name);
        return;
      }
      // Anything else is a new selection, or a deselection when the square has
      // nothing to offer -- an empty square, or the opponent's piece.
      select(puzzle.legal_moves[name] ? name : null);
    });

    reveal.addEventListener("click", function () {
      finish(false);
    });

    next.addEventListener("click", function () {
      if (index + 1 < items.length) show(index + 1);
    });

    function select(name) {
      Object.keys(squares).forEach(function (key) {
        squares[key].classList.remove("sq--from", "sq--to");
      });
      selected = name;
      if (!name) return;
      squares[name].classList.add("sq--from");
      (puzzle.legal_moves[name] || []).forEach(function (target) {
        squares[target].classList.add("sq--to");
      });
    }

    function attempt(from, to) {
      // The answer's fifth character is a promotion piece. The board always
      // promotes to a queen, so the squares are the whole comparison.
      if (from + to === puzzle.best_move.slice(0, 4)) {
        finish(true);
        return;
      }
      select(null);
      board.classList.remove("board--wrong");
      // Reading offsetWidth restarts the animation; without it a second wrong
      // move in a row does nothing visible.
      void board.offsetWidth;
      board.classList.add("board--wrong");
    }

    function play() {
      // Four elements at most, and the server worked out which. Castling moves
      // a rook as well as a king, en passant empties a square nothing landed
      // on, and a promoting pawn arrives as something else -- none of which
      // this has to know, because none of it is in the list.
      var changes = puzzle.after_move || {};
      Object.keys(changes).forEach(function (name) {
        var square = squares[name];
        if (!square) return;
        var standing = square.querySelector(".pc");
        if (standing) standing.remove();
        if (!changes[name]) return;
        var arriving = document.createElement("b");
        arriving.className = "pc pc--placed pc--" + changes[name];
        square.appendChild(arriving);
      });
    }

    function finish(solved) {
      done = true;
      select(null);
      board.classList.remove("board--wrong");
      board.classList.add(solved ? "board--solved" : "board--shown");
      play();
      // `sq--played` on both, not the picking-up pair: a destination dot is an
      // offer, and the move has already been made.
      squares[puzzle.best_move.slice(0, 2)].classList.add("sq--played");
      squares[puzzle.best_move.slice(2, 4)].classList.add("sq--played");
      outcome.textContent = solved ? "Found it." : "Here it is.";
      verdict.hidden = false;
      reveal.hidden = true;
      // No "next" on the last one: a button that ends the section by doing
      // nothing is worse than the section simply ending.
      next.hidden = index + 1 >= items.length;
    }
  }
})();
