// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/**
 * USDL — "Dolares de Luis". Token ERC-20 de PRUEBA para BNB Smart Chain testnet.
 *
 * ⚠️ SOLO PARA TESTNET. No vale nada y no debe desplegarse en mainnet: quien lo
 *    despliega puede emitir tokens sin limite.
 *
 * Existe para no depender del faucet: la tesoreria puede acuñar los USDL que
 * haga falta, cuando haga falta, sin esperar 24 horas ni topes de 10 unidades.
 *
 * Decimales: 18, igual que el USDC real de BSC. Asi el bridge se comporta
 * exactamente como lo hara en produccion (EVM_TOKEN_DECIMALS=18).
 */
contract USDL {
    string public name = "Dolares de Luis";
    string public symbol = "USDL";
    uint8 public decimals = 18;
    uint256 public totalSupply;

    address public owner;

    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    event Transfer(address indexed from, address indexed to, uint256 value);
    event Approval(address indexed owner, address indexed spender, uint256 value);

    modifier onlyOwner() {
        require(msg.sender == owner, "solo el owner");
        _;
    }

    /// Al desplegar, acredita 1.000.000 USDL a la tesoreria.
    constructor() {
        owner = msg.sender;
        _mint(msg.sender, 1_000_000 * 10 ** uint256(decimals));
    }

    /// Emite USDL nuevos hacia una cuenta de prueba. Solo la tesoreria.
    function mint(address to, uint256 amount) external onlyOwner {
        _mint(to, amount);
    }

    /// Emite a varias cuentas en una sola transaccion (ahorra gas y tiempo).
    function mintBatch(address[] calldata cuentas, uint256 amount) external onlyOwner {
        for (uint256 i = 0; i < cuentas.length; i++) {
            _mint(cuentas[i], amount);
        }
    }

    function transfer(address to, uint256 amount) external returns (bool) {
        _transfer(msg.sender, to, amount);
        return true;
    }

    function approve(address spender, uint256 amount) external returns (bool) {
        allowance[msg.sender][spender] = amount;
        emit Approval(msg.sender, spender, amount);
        return true;
    }

    function transferFrom(address from, address to, uint256 amount) external returns (bool) {
        uint256 permitido = allowance[from][msg.sender];
        require(permitido >= amount, "allowance insuficiente");
        if (permitido != type(uint256).max) {
            allowance[from][msg.sender] = permitido - amount;
        }
        _transfer(from, to, amount);
        return true;
    }

    function _transfer(address from, address to, uint256 amount) internal {
        require(to != address(0), "destino invalido");
        require(balanceOf[from] >= amount, "saldo insuficiente");
        balanceOf[from] -= amount;
        balanceOf[to] += amount;
        // Este es el evento que lee el bridge para verificar los pagos.
        emit Transfer(from, to, amount);
    }

    function _mint(address to, uint256 amount) internal {
        require(to != address(0), "destino invalido");
        totalSupply += amount;
        balanceOf[to] += amount;
        emit Transfer(address(0), to, amount);
    }
}
