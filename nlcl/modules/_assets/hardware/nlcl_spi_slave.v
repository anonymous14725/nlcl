// nlcl_spi_slave.v - NLCL SPI slave (mode 0, MSB first), oversampled by clk.
// Shipped by NLCL; synthesizable Verilog-2005.
//
// Byte protocol (shared with the NLCL embedded module):
//   master WRITE frame : 0xA5, N, then N x int16 big-endian words (field order)
//                        -> rx_words updated, rx_valid pulses for one clk
//   master READ frame  : master sends 0x5A, then clocks 1 + 2*N more bytes;
//                        the slave answers 0xA5 followed by N x int16 big-endian
//                        words taken from tx_words (snapshot when CS falls).
// Word i lives in bits [16*(N-i)-1 -: 16]  (word 0 = most significant).
// SCLK must be at most clk/8 (e.g. 1 MHz SPI with a 12+ MHz system clock).
`timescale 1ns / 1ps
`default_nettype none
module nlcl_spi_slave #(
    parameter N = 1
) (
    input  wire            clk,
    input  wire            rst,
    input  wire            sclk,
    input  wire            mosi,
    input  wire            cs_n,
    output reg             miso,
    output reg  [16*N-1:0] rx_words,
    output reg             rx_valid,
    input  wire [16*N-1:0] tx_words
);
    reg [2:0] sclk_s;
    reg [2:0] cs_s;
    reg [1:0] mosi_s;
    reg [2:0] bitcnt;
    reg [7:0] bytecnt;
    reg [7:0] shin;
    reg [7:0] shout;
    reg       reading;
    reg       frame_ok;
    reg [16*N-1:0] rx_buf;
    reg [16*N-1:0] tx_buf;

    wire       sclk_rise = (sclk_s[2:1] == 2'b01);
    wire       sclk_fall = (sclk_s[2:1] == 2'b10);
    wire       cs_active = ~cs_s[1];
    wire       cs_start  = (cs_s[2:1] == 2'b10);
    wire [7:0] byte_in   = {shin[6:0], mosi_s[1]};
    wire [7:0] tx_index  = bytecnt - 8'd1;            // data byte to send next

    always @(posedge clk) begin
        if (rst) begin
            sclk_s   <= 3'b000;
            cs_s     <= 3'b111;
            mosi_s   <= 2'b00;
            bitcnt   <= 3'd0;
            bytecnt  <= 8'd0;
            shin     <= 8'd0;
            shout    <= 8'd0;
            reading  <= 1'b0;
            frame_ok <= 1'b0;
            miso     <= 1'b0;
            rx_buf   <= {16*N{1'b0}};
            tx_buf   <= {16*N{1'b0}};
            rx_words <= {16*N{1'b0}};
            rx_valid <= 1'b0;
        end else begin
            sclk_s   <= {sclk_s[1:0], sclk};
            cs_s     <= {cs_s[1:0], cs_n};
            mosi_s   <= {mosi_s[0], mosi};
            rx_valid <= 1'b0;
            if (!cs_active) begin
                bitcnt  <= 3'd0;
                bytecnt <= 8'd0;
                reading <= 1'b0;
                shout   <= 8'd0;
                miso    <= 1'b0;
            end else begin
                if (cs_start) begin
                    tx_buf <= tx_words;
                end
                if (sclk_rise) begin
                    shin   <= byte_in;
                    bitcnt <= bitcnt + 3'd1;
                    if (bitcnt == 3'd7) begin
                        bytecnt <= bytecnt + 8'd1;
                        if (bytecnt == 8'd0) begin
                            frame_ok <= (byte_in == 8'hA5);
                            reading  <= (byte_in == 8'h5A);
                            shout    <= (byte_in == 8'h5A) ? 8'hA5 : 8'h00;
                        end else if (reading) begin
                            if (tx_index < 2*N)
                                shout <= tx_buf[16*N-1 - 8*tx_index -: 8];
                            else
                                shout <= 8'h00;
                        end else if (bytecnt == 8'd1) begin
                            if (byte_in != N) frame_ok <= 1'b0;
                        end else if (bytecnt < 2*N + 2) begin
                            rx_buf[16*N-1 - 8*(bytecnt - 8'd2) -: 8] <= byte_in;
                            if (bytecnt == 2*N + 1 && frame_ok) begin
                                rx_words <= {rx_buf[16*N-1:8], byte_in};
                                rx_valid <= 1'b1;
                            end
                        end
                    end
                end
                if (sclk_fall) begin
                    miso  <= shout[7];
                    shout <= {shout[6:0], 1'b0};
                end
            end
        end
    end
endmodule
`default_nettype wire
