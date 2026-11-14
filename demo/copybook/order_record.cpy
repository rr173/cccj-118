      *> ================================================================
      *> ORDER-FEED copybook (restricted subset understood by distagg).
      *>
      *> One record per customer order, exported from the mainframe as an
      *> RDW-framed EBCDIC (cp037) file.  This file exercises every
      *> supported construct on purpose:
      *>   - groups, elementary text and numeric items, FILLER
      *>   - DISPLAY / COMP / COMP-3 numerics, S signs, V decimal places
      *>   - a REDEFINES alternation with branches of different lengths
      *>   - a fixed OCCURS inside a variable OCCURS ... DEPENDING ON
      *>   - ordinary fields (TRAILER) after the variable-length array
      *>   - the same field name (ORDER-ID) in two different groups
      *> ================================================================
       01 ORDER-RECORD.
          05 HEADER.
             10 ORDER-ID           PIC X(8).       *> e.g. "ORD00042"
             10 ORDER-DATE         PIC 9(8).       *> YYYYMMDD, zoned DISPLAY
             10 CURRENCY           PIC X(3).       *> ISO code, space padded
             10 FILLER             PIC X(2).       *> reserved; skipped, not output
          05 AUDIT.
             10 ORDER-ID           PIC X(8).       *> audit copy of the id:
                                                   *> same name, different group
             10 OPERATOR           PIC X(4).       *> clerk that keyed the order
          05 ORDER-KIND             PIC X(1).      *> 'S' standard / 'E' extended
          05 ORDER-BODY             PIC X(6).      *> standard body (branch 1)
          05 EXTENDED-BODY REDEFINES ORDER-BODY.
             10 GIFT-FLAG          PIC X(1).       *> 'Y'/'N'
             10 LOYALTY-ID         PIC X(9).       *> longer branch: group of 10
      *> The REDEFINES group above occupies max(6, 10) = 10 bytes; the
      *> branch decoded for each record is chosen by ORDER-KIND through
      *> selectors.json, never by trial and error.
          05 DETAIL-COUNT           PIC 9(2) COMP. *> binary, 2 bytes
          05 DETAILS OCCURS 0 TO 5 DEPENDING ON ORDER-RECORD.DETAIL-COUNT.
             10 LINE-NO            PIC 9(2).       *> 01..05, zoned DISPLAY
             10 SKU                PIC X(6).
             10 QTY                PIC 9(3) COMP.  *> binary, 2 bytes
             10 UNIT-PRICE         PIC S9(5)V99 COMP-3.  *> 4 packed bytes
             10 DISCOUNT           PIC S9(3)V99 COMP-3.  *> 3 packed bytes
             10 TAGS               PIC X(2) OCCURS 3.    *> fixed array inside
      *>                                                   the variable one
          05 TRAILER.
             10 ORDER-TOTAL        PIC S9(7)V99 COMP-3.  *> 5 packed bytes
             10 STATUS-FLAG        PIC X(1).
             10 ROUTING-CODE       PIC X(2) OCCURS 3.    *> fixed array after
      *>                                                   the variable one
