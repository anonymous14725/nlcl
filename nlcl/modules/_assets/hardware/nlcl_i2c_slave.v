// nlcl_i2c_slave.v - NLCL I2C slave (7-bit address), oversampled by clk.
// Shipped by NLCL; synthesizable Verilog-2005.
//
// Byte protocol (shared with the NLCL embedded module):
//   master WRITE  : [addr+W] 0xA5, N, then N x int16 big-endian words (field order)
//                   -> rx_words updated, rx_valid pulses for one clk
//   master READ   : [addr+R] the slave answers 0xA5, N, N x int16 big-endian words
//                   taken from tx_words (snapshot at the address phase).
// SDA is open-drain: the top level does  assign sda = sda_oe ? 1'b0 : 1'bz;
// SCL must be at most clk/16 (100/400 kHz is fine with any clock >= 8 MHz).
`timescale 1ns / 1ps
`default_nettype none
module nlcl_i2c_slave #(
    parameter [6:0] ADDR = 7'h42,
    parameter N = 1
) (
    input  wire            clk,
    input  wire            rst,
    input  wire            scl,
    input  wire            sda_i,
    output reg             sda_oe,
    output reg  [16*N-1:0] rx_words,
    output reg             rx_valid,
    input  wire [16*N-1:0] tx_words
);
    localparam P_IDLE = 2'd0, P_ADDR = 2'd1, P_WRITE = 2'd2, P_READ = 2'd3;

    reg [2:0] scl_s;
    reg [2:0] sda_s;
    reg [1:0] phase;
    reg [3:0] cnt;          // SCL rising edges seen in the current byte (0..9)
    reg [7:0] shreg;
    reg [7:0] shout;
    reg [7:0] idx;          // byte index inside the frame
    reg       matched;
    reg       rw;
    reg       nack;
    reg       frame_ok;
    reg [16*N-1:0] rx_buf;
    reg [16*N-1:0] tx_buf;

    wire scl_rise = (scl_s[2:1] == 2'b01);
    wire scl_fall = (scl_s[2:1] == 2'b10);
    wire start_c  = scl_s[2] & scl_s[1] & (sda_s[2:1] == 2'b10);
    wire stop_c   = scl_s[2] & scl_s[1] & (sda_s[2:1] == 2'b01);


    // next byte to transmit in a READ frame: 0xA5, N, data bytes, then 0xFF
    wire [7:0] dsel    = idx - 8'd2;
    wire [7:0] n_byte  = N;
    wire [7:0] next_tx = (idx == 8'd0) ? 8'hA5 :
                         (idx == 8'd1) ? n_byte :
                         (dsel < 2*N)  ? tx_buf[16*N-1 - 8*dsel -: 8] : 8'hFF;

    always @(posedge clk) begin
        if (rst) begin
            scl_s    <= 3'b111;
            sda_s    <= 3'b111;
            phase    <= P_IDLE;
            cnt      <= 4'd0;
            shreg    <= 8'd0;
            shout    <= 8'hFF;
            idx      <= 8'd0;
            matched  <= 1'b0;
            rw       <= 1'b0;
            nack     <= 1'b0;
            frame_ok <= 1'b0;
            sda_oe   <= 1'b0;
            rx_buf   <= {16*N{1'b0}};
            tx_buf   <= {16*N{1'b0}};
            rx_words <= {16*N{1'b0}};
            rx_valid <= 1'b0;
        end else begin
            scl_s    <= {scl_s[1:0], scl};
            sda_s    <= {sda_s[1:0], sda_i};
            rx_valid <= 1'b0;
            if (start_c) begin
                phase   <= P_ADDR;
                cnt     <= 4'd0;
                idx     <= 8'd0;
                matched <= 1'b0;
                sda_oe  <= 1'b0;
            end else if (stop_c) begin
                phase  <= P_IDLE;
                sda_oe <= 1'b0;
            end else if (phase != P_IDLE) begin
                if (scl_rise) begin
                    if (cnt < 4'd8)
                        shreg <= {shreg[6:0], sda_s[1]};
                    else if (phase == P_READ)
                        nack <= sda_s[1];
                    cnt <= cnt + 4'd1;
                end
                if (scl_fall) begin
                    if (cnt == 4'd8) begin
                        // 8 data bits done: acknowledge (or release for the master's ACK)
                        case (phase)
                            P_ADDR: begin
                                if (shreg[7:1] == ADDR) begin
                                    sda_oe  <= 1'b1;
                                    matched <= 1'b1;
                                    rw      <= shreg[0];
                                    tx_buf  <= tx_words;
                                end else begin
                                    phase  <= P_IDLE;
                                    sda_oe <= 1'b0;
                                end
                            end
                            P_WRITE: begin
                                sda_oe <= 1'b1;
                                idx    <= idx + 8'd1;
                                if (idx == 8'd0)
                                    frame_ok <= (shreg == 8'hA5);
                                else if (idx == 8'd1) begin
                                    if (shreg != N) frame_ok <= 1'b0;
                                end else if (idx < 2*N + 2) begin
                                    rx_buf[16*N-1 - 8*(idx - 8'd2) -: 8] <= shreg;
                                    if (idx == 2*N + 1 && frame_ok) begin
                                        rx_words <= {rx_buf[16*N-1:8], shreg};
                                        rx_valid <= 1'b1;
                                    end
                                end
                            end
                            default: sda_oe <= 1'b0;   // P_READ: master drives ACK/NACK
                        endcase
                    end else if (cnt == 4'd9) begin
                        cnt <= 4'd0;
                        if (phase == P_ADDR && matched) begin
                            if (rw) begin
                                phase  <= P_READ;
                                shout  <= next_tx;
                                sda_oe <= ~next_tx[7];
                                idx    <= idx + 8'd1;
                            end else begin
                                phase  <= P_WRITE;
                                sda_oe <= 1'b0;
                            end
                        end else if (phase == P_READ) begin
                            if (nack) begin
                                phase  <= P_IDLE;
                                sda_oe <= 1'b0;
                            end else begin
                                shout  <= next_tx;
                                sda_oe <= ~next_tx[7];
                                idx    <= idx + 8'd1;
                            end
                        end else begin
                            sda_oe <= 1'b0;
                        end
                    end else if (phase == P_READ) begin
                        // bits 6..0 of the current byte
                        sda_oe <= ~shout[7 - cnt];
                    end
                end
            end
        end
    end
endmodule
`default_nettype wire
